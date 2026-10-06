"""PostgreSQL implementations of the event store and run repository."""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.errors import DomainError, InvalidTransitionError, NotFoundError
from eios_domain.events import EventDraft, EventEnvelope, EventStatus
from eios_domain.ids import new_id, utcnow
from eios_domain.run import Run, RunStatus
from eios_observability.redaction import redact, redact_text
from eios_observability.store import EventPage, RunPage
from eios_storage.tables.observability import event as event_table
from eios_storage.tables.platform import run as run_table

_MAX_PAGE = 1000


def _row_to_event(row: sa.Row[Any]) -> EventEnvelope:
    m = row._mapping
    return EventEnvelope(
        event_id=m["event_id"],
        run_id=m["run_id"],
        trace_id=m["trace_id"],
        span_id=m["span_id"],
        parent_span_id=m["parent_span_id"],
        seq=m["seq"],
        schema_version=m["schema_version"],
        type=m["type"],
        timestamp=m["timestamp"],
        actor_type=m["actor_type"],
        actor_id=m["actor_id"],
        status=m["status"],
        summary=m["summary"],
        data=m["data"],
    )


def _row_to_run(row: sa.Row[Any]) -> Run:
    m = row._mapping
    return Run(
        id=m["id"],
        trace_id=m["trace_id"],
        kind=m["kind"],
        goal=m["goal"],
        status=RunStatus(m["status"]),
        project_id=m["project_id"],
        created_at=m["created_at"],
        started_at=m["started_at"],
        finished_at=m["finished_at"],
        error=m["error"],
        metadata=m["metadata"],
    )


class PostgresEventStore:
    """Append-only event log. Per-run ``seq`` is assigned by incrementing ``platform.run.next_seq``
    in the same transaction as the insert, so it is gap-free and strictly increasing even with
    concurrent writers (the run row lock serialises appends to one run)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def append(self, draft: EventDraft) -> EventEnvelope:
        return (await self.append_many([draft]))[0]

    async def append_many(self, drafts: Sequence[EventDraft]) -> list[EventEnvelope]:
        """Atomically append drafts that all belong to the same run, with consecutive seqs."""
        if not drafts:
            return []
        run_ids = {d.run_id for d in drafts}
        if len(run_ids) != 1:
            raise DomainError("append_many requires events of a single run")
        run_id = next(iter(run_ids))
        count = len(drafts)
        async with self._engine.begin() as conn:
            last = (
                await conn.execute(
                    sa.update(run_table)
                    .where(run_table.c.id == run_id)
                    .values(next_seq=run_table.c.next_seq + count)
                    .returning(run_table.c.next_seq)
                )
            ).scalar_one_or_none()
            if last is None:
                raise NotFoundError(f"run {run_id} does not exist")
            first = last - count + 1
            now = utcnow()
            envelopes: list[EventEnvelope] = []
            for offset, draft in enumerate(drafts):
                envelopes.append(
                    EventEnvelope(
                        event_id=new_id(),
                        run_id=draft.run_id,
                        trace_id=draft.trace_id,
                        span_id=draft.span_id,
                        parent_span_id=draft.parent_span_id,
                        seq=first + offset,
                        type=draft.type,
                        timestamp=now,
                        actor_type=draft.actor_type,
                        actor_id=redact_text(draft.actor_id),
                        status=draft.status,
                        summary=redact_text(draft.summary),
                        data=redact(draft.data),
                    )
                )
            await conn.execute(
                sa.insert(event_table),
                [{**e.model_dump(mode="python"), "type": str(e.type)} for e in envelopes],
            )
        return envelopes

    async def list_events(
        self,
        run_id: uuid.UUID,
        *,
        after_seq: int = 0,
        limit: int = 100,
        types: Sequence[str] | None = None,
        status: EventStatus | None = None,
        span_id: uuid.UUID | None = None,
    ) -> EventPage:
        limit = max(1, min(limit, _MAX_PAGE))
        stmt = (
            sa.select(event_table)
            .where(event_table.c.run_id == run_id, event_table.c.seq > after_seq)
            .order_by(event_table.c.seq)
            .limit(limit + 1)
        )
        if types:
            stmt = stmt.where(event_table.c.type.in_(list(types)))
        if status is not None:
            stmt = stmt.where(event_table.c.status == status.value)
        if span_id is not None:
            stmt = stmt.where(event_table.c.span_id == span_id)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        has_more = len(rows) > limit
        items = [_row_to_event(r) for r in rows[:limit]]
        return EventPage(
            items=items,
            has_more=has_more,
            next_cursor=items[-1].seq if items and has_more else None,
        )

    async def latest_seq(self, run_id: uuid.UUID) -> int:
        async with self._engine.connect() as conn:
            value = (
                await conn.execute(sa.select(run_table.c.next_seq).where(run_table.c.id == run_id))
            ).scalar_one_or_none()
        if value is None:
            raise NotFoundError(f"run {run_id} does not exist")
        return int(value)


def _encode_cursor(created_at: datetime, run_id: uuid.UUID) -> str:
    raw = json.dumps({"t": created_at.isoformat(), "i": str(run_id)}).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(data["t"]), uuid.UUID(data["i"])
    except Exception as exc:
        raise DomainError("invalid cursor") from exc


class PostgresRunRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create(self, run: Run, *, budget: dict[str, object] | None = None) -> Run:
        async with self._engine.begin() as conn:
            await conn.execute(
                pg_insert(run_table).values(
                    id=run.id,
                    trace_id=run.trace_id,
                    kind=run.kind,
                    goal=redact_text(run.goal),
                    status=run.status.value,
                    project_id=run.project_id,
                    created_at=run.created_at,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                    error=run.error,
                    metadata=redact(run.metadata),
                    budget=budget,
                )
            )
        return run

    async def get(self, run_id: uuid.UUID) -> Run | None:
        async with self._engine.connect() as conn:
            row = (await conn.execute(sa.select(run_table).where(run_table.c.id == run_id))).first()
        return _row_to_run(row) if row else None

    async def save_transition(self, run: Run, *, expected: RunStatus) -> Run:
        """Persist a status change only if the stored status is still ``expected``."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                sa.update(run_table)
                .where(run_table.c.id == run.id, run_table.c.status == expected.value)
                .values(
                    status=run.status.value,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                    error=redact_text(run.error) if run.error else None,
                )
            )
            if result.rowcount != 1:
                raise InvalidTransitionError(
                    f"run {run.id} is no longer {expected.value}; concurrent update or missing run"
                )
        return run

    async def list_runs(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        status: RunStatus | None = None,
        created_before: datetime | None = None,
    ) -> RunPage:
        limit = max(1, min(limit, 200))
        stmt = sa.select(run_table).order_by(run_table.c.created_at.desc(), run_table.c.id.desc())
        if status is not None:
            stmt = stmt.where(run_table.c.status == status.value)
        if created_before is not None:
            stmt = stmt.where(run_table.c.created_at < created_before)
        if cursor:
            ts, rid = _decode_cursor(cursor)
            stmt = stmt.where(
                sa.tuple_(run_table.c.created_at, run_table.c.id) < sa.tuple_(ts, rid)
            )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt.limit(limit + 1))).all()
        items = [_row_to_run(r) for r in rows[:limit]]
        next_cursor = (
            _encode_cursor(items[-1].created_at, items[-1].id) if len(rows) > limit else None
        )
        return RunPage(items=items, next_cursor=next_cursor)
