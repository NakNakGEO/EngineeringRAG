"""SQL for governance state: trust/health updates with history, dependencies, contradictions."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from eios_domain.ids import new_id, utcnow
from eios_storage.tables.governance import contradiction as contradiction_t
from eios_storage.tables.governance import dependency as dependency_t
from eios_storage.tables.governance import lifecycle as lifecycle_t
from eios_storage.tables.knowledge import item as item_t


class GovernanceStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    # -- history --
    @staticmethod
    async def record(
        conn: AsyncConnection,
        *,
        subject_kind: str,
        subject_id: uuid.UUID,
        change: str,
        from_value: str | None,
        to_value: str,
        actor: str,
        reason: str = "",
        evidence_ids: Sequence[uuid.UUID] = (),
        approval: dict[str, Any] | None = None,
    ) -> None:
        await conn.execute(
            sa.insert(lifecycle_t).values(
                id=new_id(),
                subject_kind=subject_kind,
                subject_id=subject_id,
                change=change,
                from_value=from_value,
                to_value=to_value,
                actor=actor[:200],
                reason=reason[:1000],
                evidence_ids=[str(e) for e in evidence_ids],
                approval=approval,
                at=utcnow(),
            )
        )

    async def history(
        self, subject_kind: str, subject_id: uuid.UUID, limit: int = 100
    ) -> list[dict[str, Any]]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.select(lifecycle_t)
                    .where(
                        lifecycle_t.c.subject_kind == subject_kind,
                        lifecycle_t.c.subject_id == subject_id,
                    )
                    .order_by(lifecycle_t.c.at.desc())
                    .limit(limit)
                )
            ).all()
        return [dict(r._mapping) for r in rows]

    # -- item state --
    async def apply_item_change(
        self,
        item_id: uuid.UUID,
        *,
        change: str,
        values: dict[str, Any],
        from_value: str | None,
        to_value: str,
        actor: str,
        reason: str,
        evidence_ids: Sequence[uuid.UUID] = (),
        approval: dict[str, Any] | None = None,
        expected: dict[str, Any] | None = None,
    ) -> bool:
        """Update an item and write its history row atomically.

        ``expected`` makes it compare-and-set (e.g. ``{"health": "CURRENT"}``): returns False when
        the item no longer has that state, so concurrent governance never overwrites a newer one.
        """
        async with self._engine.begin() as conn:
            stmt = sa.update(item_t).where(item_t.c.id == item_id)
            for column, value in (expected or {}).items():
                stmt = stmt.where(item_t.c[column] == value)
            result = await conn.execute(stmt.values(updated_at=utcnow(), **values))
            if result.rowcount != 1:
                return False
            await self.record(
                conn,
                subject_kind="item",
                subject_id=item_id,
                change=change,
                from_value=from_value,
                to_value=to_value,
                actor=actor,
                reason=reason,
                evidence_ids=evidence_ids,
                approval=approval,
            )
        return True

    # -- dependencies --
    async def set_dependencies(
        self,
        item_id: uuid.UUID,
        project_id: uuid.UUID | None,
        deps: Sequence[tuple[str, str, str | None]],
    ) -> int:
        """Replace the item's dependency set with ``(kind, key, hash)`` entries."""
        now = utcnow()
        async with self._engine.begin() as conn:
            await conn.execute(sa.delete(dependency_t).where(dependency_t.c.item_id == item_id))
            unique = {(k, key): h for k, key, h in deps}
            if unique:
                await conn.execute(
                    sa.insert(dependency_t),
                    [
                        {
                            "item_id": item_id,
                            "dep_kind": k,
                            "dep_key": key,
                            "dep_hash": h,
                            "project_id": project_id,
                            "created_at": now,
                        }
                        for (k, key), h in unique.items()
                    ],
                )
        return len(unique)

    async def dependencies(self, item_id: uuid.UUID) -> list[dict[str, Any]]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(sa.select(dependency_t).where(dependency_t.c.item_id == item_id))
            ).all()
        return [dict(r._mapping) for r in rows]

    async def dependents_of(
        self, project_id: uuid.UUID | None, dep_kind: str, keys: Sequence[str]
    ) -> list[dict[str, Any]]:
        """Items (with their current health) that depend on any of ``keys``."""
        if not keys:
            return []
        stmt = (
            sa.select(
                item_t.c.id,
                item_t.c.health,
                item_t.c.trust,
                item_t.c.vault,
                dependency_t.c.dep_key,
                dependency_t.c.dep_hash,
            )
            .join(item_t, item_t.c.id == dependency_t.c.item_id)
            .where(dependency_t.c.dep_kind == dep_kind, dependency_t.c.dep_key.in_(list(keys)))
        )
        if project_id is not None:
            stmt = stmt.where(dependency_t.c.project_id == project_id)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [dict(r._mapping) for r in rows]

    # -- contradictions --
    async def add_contradiction(
        self, item_a: uuid.UUID, item_b: uuid.UUID, reason: str, detected_by: str
    ) -> uuid.UUID | None:
        a, b = sorted((item_a, item_b))
        cid = new_id()
        stmt = (
            pg_insert(contradiction_t)
            .values(
                id=cid,
                item_a=a,
                item_b=b,
                reason=reason[:1000],
                status="open",
                detected_by=detected_by[:200],
                created_at=utcnow(),
            )
            .on_conflict_do_nothing(constraint="uq_contradiction_pair")
            .returning(contradiction_t.c.id)
        )
        async with self._engine.begin() as conn:
            row = (await conn.execute(stmt)).first()
        return row[0] if row else None

    async def contradictions(
        self, status: str | None = "open", limit: int = 100
    ) -> list[dict[str, Any]]:
        stmt = sa.select(contradiction_t).order_by(contradiction_t.c.created_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(contradiction_t.c.status == status)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [dict(r._mapping) for r in rows]

    async def resolve_contradiction(
        self, contradiction_id: uuid.UUID, resolved_by: str, resolution: str
    ) -> bool:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                sa.update(contradiction_t)
                .where(contradiction_t.c.id == contradiction_id, contradiction_t.c.status == "open")
                .values(
                    status="resolved",
                    resolved_by=resolved_by[:200],
                    resolution=resolution[:1000],
                    resolved_at=utcnow(),
                )
            )
        return result.rowcount == 1

    async def open_contradictions_for(self, item_id: uuid.UUID) -> int:
        async with self._engine.connect() as conn:
            return int(
                (
                    await conn.execute(
                        sa.select(sa.func.count())
                        .select_from(contradiction_t)
                        .where(
                            contradiction_t.c.status == "open",
                            sa.or_(
                                contradiction_t.c.item_a == item_id,
                                contradiction_t.c.item_b == item_id,
                            ),
                        )
                    )
                ).scalar_one()
            )
