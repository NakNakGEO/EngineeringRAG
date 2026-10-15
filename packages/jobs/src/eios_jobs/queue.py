"""Job queue port and its PostgreSQL implementation (leases + ``FOR UPDATE SKIP LOCKED``).

The queue is behind :class:`JobQueue` so it can be swapped for a broker later without touching
handlers. All lease arithmetic uses the database clock (one authority across workers).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.ids import new_id
from eios_observability.redaction import redact, redact_text
from eios_storage.tables.platform import job as job_t


class Job(BaseModel):
    id: uuid.UUID
    type: str
    status: str
    payload: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    lease_owner: str | None
    lease_until: datetime | None
    attempts: int
    max_attempts: int
    available_at: datetime
    idempotency_key: str | None
    run_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class JobQueue(Protocol):
    async def enqueue(
        self,
        type_: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        delay_seconds: float = 0,
        max_attempts: int = 3,
        run_id: uuid.UUID | None = None,
    ) -> Job: ...

    async def claim(
        self, worker_id: str, *, types: Sequence[str], lease_seconds: float
    ) -> Job | None: ...

    async def heartbeat(self, job_id: uuid.UUID, worker_id: str, lease_seconds: float) -> bool: ...

    async def complete(
        self, job_id: uuid.UUID, worker_id: str, result: dict[str, Any] | None = None
    ) -> bool: ...

    async def fail(
        self,
        job_id: uuid.UUID,
        worker_id: str,
        error: str,
        *,
        retry_delay_seconds: float = 0,
        permanent: bool = False,
    ) -> Job | None: ...

    async def reclaim_expired(self) -> int: ...

    async def find_active(self, type_: str, payload_match: dict[str, Any]) -> Job | None: ...

    async def get(self, job_id: uuid.UUID) -> Job | None: ...


def _job(row: Any) -> Job:
    return Job(**dict(row._mapping))


def _seconds(value: float) -> Any:
    return sa.func.make_interval(0, 0, 0, 0, 0, 0, value)


class PostgresJobQueue:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def enqueue(
        self,
        type_: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        delay_seconds: float = 0,
        max_attempts: int = 3,
        run_id: uuid.UUID | None = None,
    ) -> Job:
        """Enqueue a job. With an ``idempotency_key`` an existing (type, key) job is returned."""
        values = {
            "id": new_id(),
            "type": type_,
            "status": "queued",
            "payload": redact(payload or {}),
            "attempts": 0,
            "max_attempts": max_attempts,
            "available_at": sa.func.now() + _seconds(delay_seconds),
            "idempotency_key": idempotency_key,
            "run_id": run_id,
            "created_at": sa.func.now(),
            "updated_at": sa.func.now(),
        }
        async with self._engine.begin() as conn:
            stmt = pg_insert(job_t).values(**values)
            if idempotency_key is not None:
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["type", "idempotency_key"],
                    index_where=sa.text("idempotency_key IS NOT NULL"),
                )
            row = (await conn.execute(stmt.returning(job_t))).first()
            if row is None:  # idempotent hit
                row = (
                    await conn.execute(
                        sa.select(job_t).where(
                            job_t.c.type == type_, job_t.c.idempotency_key == idempotency_key
                        )
                    )
                ).one()
        return _job(row)

    async def claim(
        self, worker_id: str, *, types: Sequence[str], lease_seconds: float
    ) -> Job | None:
        nxt = (
            sa.select(job_t.c.id)
            .where(
                job_t.c.status == "queued",
                job_t.c.available_at <= sa.func.now(),
                job_t.c.type.in_(list(types)),
            )
            .order_by(job_t.c.available_at, job_t.c.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        stmt = (
            sa.update(job_t)
            .where(job_t.c.id == nxt)
            .values(
                status="running",
                lease_owner=worker_id,
                lease_until=sa.func.now() + _seconds(lease_seconds),
                attempts=job_t.c.attempts + 1,
                updated_at=sa.func.now(),
            )
            .returning(job_t)
        )
        async with self._engine.begin() as conn:
            row = (await conn.execute(stmt)).first()
        return _job(row) if row else None

    async def heartbeat(self, job_id: uuid.UUID, worker_id: str, lease_seconds: float) -> bool:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                sa.update(job_t)
                .where(
                    job_t.c.id == job_id,
                    job_t.c.lease_owner == worker_id,
                    job_t.c.status == "running",
                )
                .values(
                    lease_until=sa.func.now() + _seconds(lease_seconds), updated_at=sa.func.now()
                )
            )
        return result.rowcount == 1

    async def complete(
        self, job_id: uuid.UUID, worker_id: str, result: dict[str, Any] | None = None
    ) -> bool:
        async with self._engine.begin() as conn:
            res = await conn.execute(
                sa.update(job_t)
                .where(
                    job_t.c.id == job_id,
                    job_t.c.lease_owner == worker_id,
                    job_t.c.status == "running",
                )
                .values(
                    status="succeeded",
                    result=redact(result or {}),
                    lease_owner=None,
                    lease_until=None,
                    error=None,
                    updated_at=sa.func.now(),
                )
            )
        return res.rowcount == 1

    async def fail(
        self,
        job_id: uuid.UUID,
        worker_id: str,
        error: str,
        *,
        retry_delay_seconds: float = 0,
        permanent: bool = False,
    ) -> Job | None:
        """Record a failure: requeue with a delay, or ``dead`` when attempts are spent/permanent."""
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    sa.update(job_t)
                    .where(
                        job_t.c.id == job_id,
                        job_t.c.lease_owner == worker_id,
                        job_t.c.status == "running",
                    )
                    .values(
                        status=sa.case(
                            (sa.literal(permanent), "dead"),
                            (job_t.c.attempts >= job_t.c.max_attempts, "dead"),
                            else_="queued",
                        ),
                        error=redact_text(error)[:2000],
                        lease_owner=None,
                        lease_until=None,
                        available_at=sa.func.now() + _seconds(retry_delay_seconds),
                        updated_at=sa.func.now(),
                    )
                    .returning(job_t)
                )
            ).first()
        return _job(row) if row else None

    async def reclaim_expired(self) -> int:
        """Return jobs whose worker vanished (lease expired) to the queue, or to ``dead``."""
        async with self._engine.begin() as conn:
            res = await conn.execute(
                sa.update(job_t)
                .where(job_t.c.status == "running", job_t.c.lease_until < sa.func.now())
                .values(
                    status=sa.case(
                        (job_t.c.attempts >= job_t.c.max_attempts, "dead"), else_="queued"
                    ),
                    error="lease expired (worker lost)",
                    lease_owner=None,
                    lease_until=None,
                    available_at=sa.func.now(),
                    updated_at=sa.func.now(),
                )
            )
        return int(res.rowcount)

    async def find_active(self, type_: str, payload_match: dict[str, Any]) -> Job | None:
        """The queued or running job of ``type_`` whose payload contains ``payload_match``."""
        stmt = (
            sa.select(job_t)
            .where(
                job_t.c.type == type_,
                job_t.c.status.in_(["queued", "running"]),
                job_t.c.payload.contains(payload_match),
            )
            .order_by(job_t.c.created_at)
            .limit(1)
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(stmt)).first()
        return _job(row) if row else None

    async def get(self, job_id: uuid.UUID) -> Job | None:
        async with self._engine.connect() as conn:
            row = (await conn.execute(sa.select(job_t).where(job_t.c.id == job_id))).first()
        return _job(row) if row else None

    async def list_jobs(
        self, *, status: str | None = None, type_: str | None = None, limit: int = 100
    ) -> list[Job]:
        stmt = sa.select(job_t).order_by(job_t.c.created_at.desc()).limit(max(1, min(limit, 500)))
        if status:
            stmt = stmt.where(job_t.c.status == status)
        if type_:
            stmt = stmt.where(job_t.c.type == type_)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [_job(r) for r in rows]
