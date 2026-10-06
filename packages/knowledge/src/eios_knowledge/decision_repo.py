"""Decision ledger persistence (master plan 6.18)."""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.errors import DomainError, NotFoundError, require_found
from eios_domain.ids import new_id, utcnow
from eios_domain.knowledge import Decision, DecisionCreate, DecisionStatus
from eios_domain.vault import Vault
from eios_knowledge.scope import SearchScope, scope_clause
from eios_storage.tables.knowledge import decision as decision_t

_COLS = [c for c in decision_t.c if c.name != "search_vector"]


def _decision(row: Any) -> Decision:
    m = row._mapping
    return Decision(
        id=m["id"],
        vault=Vault(m["vault"]),
        project_id=m["project_id"],
        question=m["question"],
        alternatives=m["alternatives"],
        selected=m["selected"],
        rationale=m["rationale"],
        evidence_ids=list(m["evidence_ids"]),
        decider=m["decider"],
        reviewers=list(m["reviewers"]),
        status=DecisionStatus(m["status"]),
        superseded_by=m["superseded_by"],
        created_at=m["created_at"],
        updated_at=m["updated_at"],
    )


class DecisionRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create(self, create: DecisionCreate) -> Decision:
        decision_id, now = new_id(), utcnow()
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.insert(decision_t).values(
                    id=decision_id,
                    vault=create.vault.value,
                    project_id=create.project_id,
                    question=create.question,
                    alternatives=create.alternatives,
                    selected=create.selected,
                    rationale=create.rationale,
                    evidence_ids=create.evidence_ids,
                    decider=create.decider,
                    reviewers=create.reviewers,
                    status=create.status.value,
                    created_at=now,
                    updated_at=now,
                )
            )
        return require_found(await self.get(decision_id), "record")

    async def get(self, decision_id: uuid.UUID) -> Decision | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(sa.select(*_COLS).where(decision_t.c.id == decision_id))
            ).first()
        return _decision(row) if row else None

    async def set_status(self, decision_id: uuid.UUID, status: DecisionStatus) -> Decision:
        """Move between PROPOSED / ACCEPTED / REJECTED. Use :meth:`supersede` for SUPERSEDED."""
        if status is DecisionStatus.SUPERSEDED:
            raise DomainError("use supersede() to mark a decision as superseded")
        current = await self.get(decision_id)
        if current is None:
            raise NotFoundError(f"decision {decision_id} not found")
        if current.status is DecisionStatus.SUPERSEDED:
            raise DomainError("a superseded decision is historical and cannot change status")
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(decision_t)
                .where(decision_t.c.id == decision_id)
                .values(status=status.value, updated_at=utcnow())
            )
        return require_found(await self.get(decision_id), "record")

    async def supersede(self, old_id: uuid.UUID, new_id_: uuid.UUID) -> Decision:
        old, new = await self.get(old_id), await self.get(new_id_)
        if old is None or new is None:
            raise NotFoundError("decision not found")
        if old_id == new_id_:
            raise DomainError("a decision cannot supersede itself")
        if old.status is DecisionStatus.SUPERSEDED:
            raise DomainError("decision is already superseded")
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(decision_t)
                .where(decision_t.c.id == old_id)
                .values(
                    status=DecisionStatus.SUPERSEDED.value,
                    superseded_by=new_id_,
                    updated_at=utcnow(),
                )
            )
        return require_found(await self.get(old_id), "record")

    async def search_text(
        self, query: str, scope: SearchScope, *, limit: int = 20
    ) -> list[tuple[Decision, float]]:
        tsquery = sa.func.websearch_to_tsquery("english", query)
        rank = sa.func.ts_rank_cd(decision_t.c.search_vector, tsquery).label("score")
        stmt = (
            sa.select(*_COLS, rank)
            .where(
                decision_t.c.search_vector.op("@@")(tsquery),
                scope_clause(decision_t, scope, has_health=False),
            )
            .order_by(sa.desc("score"), decision_t.c.id)
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [(_decision(r), float(r._mapping["score"])) for r in rows]
