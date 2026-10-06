"""Memory item persistence."""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.errors import require_found
from eios_domain.ids import new_id, utcnow
from eios_domain.knowledge import MemoryCreate
from eios_domain.vault import Vault
from eios_knowledge.scope import SearchScope, scope_clause
from eios_storage.tables.memory import item as memory_t

_COLS = [c for c in memory_t.c if c.name not in {"embedding", "search_vector"}]


class MemoryItem(BaseModel):
    id: uuid.UUID
    vault: Vault
    project_id: uuid.UUID | None
    scope: str
    content: str
    tags: list[str]
    importance: float
    created_by: str


def _item(row: Any) -> MemoryItem:
    m = row._mapping
    return MemoryItem(
        id=m["id"],
        vault=Vault(m["vault"]),
        project_id=m["project_id"],
        scope=m["scope"],
        content=m["content"],
        tags=list(m["tags"]),
        importance=m["importance"],
        created_by=m["created_by"],
    )


class MemoryRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add(
        self,
        create: MemoryCreate,
        *,
        embedding: list[float] | None = None,
        embedding_model: str | None = None,
    ) -> MemoryItem:
        item_id = new_id()
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.insert(memory_t).values(
                    id=item_id,
                    vault=create.vault.value,
                    project_id=create.project_id,
                    scope=create.scope,
                    content=create.content,
                    tags=create.tags,
                    importance=create.importance,
                    created_by=create.created_by,
                    created_at=utcnow(),
                    expires_at=create.expires_at,
                    embedding=embedding,
                    embedding_model=embedding_model,
                )
            )
        return require_found(await self.get(item_id), "record")

    async def get(self, item_id: uuid.UUID) -> MemoryItem | None:
        async with self._engine.connect() as conn:
            row = (await conn.execute(sa.select(*_COLS).where(memory_t.c.id == item_id))).first()
        return _item(row) if row else None

    async def search_text(
        self, query: str, scope: SearchScope, *, limit: int = 20
    ) -> list[tuple[MemoryItem, float]]:
        tsquery = sa.func.websearch_to_tsquery("english", query)
        rank = sa.func.ts_rank_cd(memory_t.c.search_vector, tsquery).label("score")
        stmt = (
            sa.select(*_COLS, rank)
            .where(
                memory_t.c.search_vector.op("@@")(tsquery),
                scope_clause(memory_t, scope, has_health=False),
            )
            .order_by(sa.desc("score"), memory_t.c.id)
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [(_item(r), float(r._mapping["score"])) for r in rows]

    async def search_vector(
        self, embedding: list[float], scope: SearchScope, *, limit: int = 20
    ) -> list[tuple[MemoryItem, float]]:
        distance = memory_t.c.embedding.cosine_distance(embedding)
        stmt = (
            sa.select(*_COLS, (1 - distance).label("score"))
            .where(
                memory_t.c.embedding.is_not(None), scope_clause(memory_t, scope, has_health=False)
            )
            .order_by(distance, memory_t.c.id)
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [(_item(r), float(r._mapping["score"])) for r in rows]
