"""Knowledge item persistence and low-level search (FTS and vector) with vault scoping."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.errors import DomainError, NotFoundError, require_found
from eios_domain.ids import new_id, utcnow
from eios_domain.knowledge import (
    Health,
    HumanApproval,
    KnowledgeItem,
    KnowledgeItemCreate,
    SourceKind,
    Trust,
)
from eios_domain.vault import Vault
from eios_knowledge.scope import SearchScope, scope_clause
from eios_storage.tables.knowledge import item as item_t
from eios_storage.tables.knowledge import provenance as provenance_t

_ITEM_COLUMNS = [c for c in item_t.c if c.name not in {"embedding", "search_vector"}]


def row_to_item(row: Any) -> KnowledgeItem:
    m = row._mapping
    return KnowledgeItem(
        id=m["id"],
        vault=Vault(m["vault"]),
        project_id=m["project_id"],
        kind=m["kind"],
        title=m["title"],
        content=m["content"],
        tags=list(m["tags"]),
        trust=Trust(m["trust"]),
        health=Health(m["health"]),
        confidence=m["confidence"],
        source_kind=SourceKind(m["source_kind"]),
        created_by=m["created_by"],
        created_at=m["created_at"],
        updated_at=m["updated_at"],
        expires_at=m["expires_at"],
        subject_key=m["subject_key"],
        limitations=m["limitations"],
        version_ref=m["version_ref"],
        superseded_by=m["superseded_by"],
        metadata=m["metadata"],
    )


class KnowledgeRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add(
        self,
        create: KnowledgeItemCreate,
        *,
        embedding: list[float] | None = None,
        embedding_model: str | None = None,
    ) -> KnowledgeItem:
        item_id, now = new_id(), utcnow()
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.insert(item_t).values(
                    id=item_id,
                    vault=create.vault.value,
                    project_id=create.project_id,
                    kind=create.kind,
                    title=create.title,
                    content=create.content,
                    tags=create.tags,
                    trust=create.trust.value,
                    health=create.health.value,
                    confidence=create.confidence,
                    source_kind=create.source_kind.value,
                    created_by=create.created_by,
                    created_at=now,
                    updated_at=now,
                    expires_at=create.expires_at,
                    subject_key=create.subject_key,
                    limitations=create.limitations,
                    version_ref=create.version_ref,
                    metadata=create.metadata,
                    embedding=embedding,
                    embedding_model=embedding_model,
                )
            )
            if create.provenance:
                await conn.execute(
                    sa.insert(provenance_t),
                    [
                        {
                            "id": new_id(),
                            "item_id": item_id,
                            "source": p.source,
                            "source_version": p.source_version,
                            "actor": p.actor,
                            "evidence_id": p.evidence_id,
                            "evidence_hash": p.evidence_hash,
                            "notes": p.notes,
                            "created_at": now,
                        }
                        for p in create.provenance
                    ],
                )
        return require_found(await self.get(item_id), "record")

    async def get(self, item_id: uuid.UUID) -> KnowledgeItem | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(sa.select(*_ITEM_COLUMNS).where(item_t.c.id == item_id))
            ).first()
        return row_to_item(row) if row else None

    async def get_many(self, ids: list[uuid.UUID]) -> list[KnowledgeItem]:
        if not ids:
            return []
        async with self._engine.connect() as conn:
            rows = (await conn.execute(sa.select(*_ITEM_COLUMNS).where(item_t.c.id.in_(ids)))).all()
        by_id = {r._mapping["id"]: row_to_item(r) for r in rows}
        return [by_id[i] for i in ids if i in by_id]

    async def provenance(self, item_id: uuid.UUID) -> list[dict[str, Any]]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.select(provenance_t)
                    .where(provenance_t.c.item_id == item_id)
                    .order_by(provenance_t.c.created_at)
                )
            ).all()
        return [dict(r._mapping) for r in rows]

    @staticmethod
    def _filters(
        kinds: Sequence[str] | None,
        source_kinds: Sequence[SourceKind] | None,
        exclude_kinds: Sequence[str] | None = None,
    ) -> list[sa.ColumnElement[bool]]:
        out: list[sa.ColumnElement[bool]] = []
        if kinds:
            out.append(item_t.c.kind.in_(list(kinds)))
        if exclude_kinds:
            out.append(item_t.c.kind.notin_(list(exclude_kinds)))
        if source_kinds:
            out.append(item_t.c.source_kind.in_([s.value for s in source_kinds]))
        return out

    async def search_text(
        self,
        query: str,
        scope: SearchScope,
        *,
        limit: int = 20,
        kinds: Sequence[str] | None = None,
        source_kinds: Sequence[SourceKind] | None = None,
        exclude_kinds: Sequence[str] | None = None,
    ) -> list[tuple[KnowledgeItem, float]]:
        """PostgreSQL full-text search. Returns (item, ts_rank) best first."""
        tsquery = sa.func.websearch_to_tsquery("english", query)
        rank = sa.func.ts_rank_cd(item_t.c.search_vector, tsquery).label("score")
        stmt = (
            sa.select(*_ITEM_COLUMNS, rank)
            .where(
                item_t.c.search_vector.op("@@")(tsquery),
                scope_clause(item_t, scope),
                *self._filters(kinds, source_kinds, exclude_kinds),
            )
            .order_by(sa.desc("score"), item_t.c.id)
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [(row_to_item(r), float(r._mapping["score"])) for r in rows]

    async def search_vector(
        self,
        embedding: list[float],
        scope: SearchScope,
        *,
        limit: int = 20,
        kinds: Sequence[str] | None = None,
        source_kinds: Sequence[SourceKind] | None = None,
        exclude_kinds: Sequence[str] | None = None,
    ) -> list[tuple[KnowledgeItem, float]]:
        """Cosine-similarity search (pgvector). Returns (item, similarity in [-1, 1])."""
        distance = item_t.c.embedding.cosine_distance(embedding)
        stmt = (
            sa.select(*_ITEM_COLUMNS, (1 - distance).label("score"))
            .where(
                item_t.c.embedding.is_not(None),
                scope_clause(item_t, scope),
                *self._filters(kinds, source_kinds, exclude_kinds),
            )
            .order_by(distance, item_t.c.id)
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [(row_to_item(r), float(r._mapping["score"])) for r in rows]

    async def move_to_default_vault(
        self, item_id: uuid.UUID, approval: HumanApproval
    ) -> KnowledgeItem:
        """Project Vault -> Default Vault requires an explicit human decision (never automatic)."""
        item = await self.get(item_id)
        if item is None:
            raise NotFoundError(f"knowledge item {item_id} not found")
        if item.vault is not Vault.PROJECT:
            raise DomainError("only project vault items can be moved to the default vault")
        now = utcnow()
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(item_t)
                .where(item_t.c.id == item_id, item_t.c.vault == Vault.PROJECT.value)
                .values(vault=Vault.DEFAULT.value, project_id=None, expires_at=None, updated_at=now)
            )
            await conn.execute(
                sa.insert(provenance_t).values(
                    id=new_id(),
                    item_id=item_id,
                    source=f"vault-move:project:{item.project_id}",
                    actor=approval.approver,
                    notes=f"human approval {approval.approval_id}: {approval.reason}"[:1000],
                    created_at=now,
                )
            )
        return require_found(await self.get(item_id), "record")
