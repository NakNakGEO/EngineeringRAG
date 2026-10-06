"""SQL persistence for workshop proposals and their history."""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.ids import new_id, utcnow
from eios_storage.tables.workshop import history as history_t
from eios_storage.tables.workshop import proposal as proposal_t
from eios_workshop.models import Proposal, ProposalKind, ProposalState


class ProposalConflictError(Exception):
    """Duplicate name/version, or the proposal is not in the state the caller expected."""


class WorkshopStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create(self, values: dict[str, Any], *, actor: str) -> uuid.UUID:
        pid, now = new_id(), utcnow()
        async with self._engine.begin() as conn:
            clash = (
                await conn.execute(
                    sa.select(proposal_t.c.id).where(
                        proposal_t.c.kind == values["kind"],
                        proposal_t.c.name == values["name"],
                        proposal_t.c.version == values["version"],
                        proposal_t.c.state != ProposalState.REJECTED.value,
                    )
                )
            ).first()
            if clash is not None:
                raise ProposalConflictError(
                    f"{values['kind']} '{values['name']}' {values['version']} already exists; "
                    "publish a new version instead"
                )
            await conn.execute(
                sa.insert(proposal_t).values(
                    id=pid,
                    state=ProposalState.DRAFT.value,
                    created_at=now,
                    updated_at=now,
                    **values,
                )
            )
            await self._history(conn, pid, None, ProposalState.DRAFT, actor, "proposal created")
        return pid

    @staticmethod
    async def _history(
        conn: Any,
        pid: uuid.UUID,
        old: ProposalState | None,
        new: ProposalState,
        actor: str,
        reason: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        await conn.execute(
            sa.insert(history_t).values(
                id=new_id(),
                proposal_id=pid,
                from_state=old.value if old else None,
                to_state=new.value,
                actor=actor[:200],
                reason=reason[:1000],
                detail=detail or {},
                at=utcnow(),
            )
        )

    async def get(self, pid: uuid.UUID) -> Proposal | None:
        async with self._engine.connect() as conn:
            row = (await conn.execute(sa.select(proposal_t).where(proposal_t.c.id == pid))).first()
        return self._model(row) if row else None

    async def list_proposals(
        self, *, state: ProposalState | None = None, limit: int = 50
    ) -> list[Proposal]:
        stmt = sa.select(proposal_t).order_by(proposal_t.c.created_at.desc()).limit(limit)
        if state is not None:
            stmt = stmt.where(proposal_t.c.state == state.value)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [self._model(r) for r in rows]

    async def history(self, pid: uuid.UUID) -> list[dict[str, Any]]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.select(history_t)
                    .where(history_t.c.proposal_id == pid)
                    .order_by(history_t.c.at)
                )
            ).all()
        return [dict(r._mapping) for r in rows]

    async def advance(
        self,
        pid: uuid.UUID,
        *,
        expected: ProposalState,
        to: ProposalState,
        actor: str,
        reason: str,
        fields: dict[str, Any] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Compare-and-set the state; ``fields`` are written in the same transaction."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                sa.update(proposal_t)
                .where(proposal_t.c.id == pid, proposal_t.c.state == expected.value)
                .values(state=to.value, updated_at=utcnow(), **(fields or {}))
            )
            if result.rowcount != 1:
                raise ProposalConflictError(f"proposal is no longer {expected.value}")
            await self._history(conn, pid, expected, to, actor, reason, detail)

    async def annotate(self, pid: uuid.UUID, fields: dict[str, Any]) -> None:
        """Record results (test runs, scans) without changing state."""
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(proposal_t)
                .where(proposal_t.c.id == pid)
                .values(updated_at=utcnow(), **fields)
            )

    @staticmethod
    def _model(row: Any) -> Proposal:
        m = dict(row._mapping)
        m["kind"] = ProposalKind(m["kind"])
        m["state"] = ProposalState(m["state"])
        return Proposal(**m)
