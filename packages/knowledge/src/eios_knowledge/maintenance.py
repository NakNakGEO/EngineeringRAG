"""Ephemeral workspace cleanup: expired rows and orphaned blobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.ids import utcnow
from eios_knowledge.blob_store import BlobStore
from eios_storage.tables.evidence import blob as blob_t
from eios_storage.tables.evidence import record as record_t
from eios_storage.tables.knowledge import item as knowledge_t
from eios_storage.tables.memory import item as memory_t


@dataclass(frozen=True)
class PurgeResult:
    knowledge_items: int
    memory_items: int
    evidence_records: int
    blobs: int


async def purge_expired(
    engine: AsyncEngine, blobs: BlobStore, *, now: datetime | None = None
) -> PurgeResult:
    """Delete everything whose TTL has passed, then blobs nobody references any more."""
    now = now or utcnow()
    async with engine.begin() as conn:
        k = (
            await conn.execute(
                sa.delete(knowledge_t).where(
                    knowledge_t.c.expires_at.is_not(None), knowledge_t.c.expires_at <= now
                )
            )
        ).rowcount
        m = (
            await conn.execute(
                sa.delete(memory_t).where(
                    memory_t.c.expires_at.is_not(None), memory_t.c.expires_at <= now
                )
            )
        ).rowcount
        e = (
            await conn.execute(
                sa.delete(record_t).where(
                    record_t.c.expires_at.is_not(None), record_t.c.expires_at <= now
                )
            )
        ).rowcount
        orphans = (
            await conn.execute(
                sa.select(blob_t.c.id, blob_t.c.sha256).where(
                    ~sa.exists().where(record_t.c.blob_id == blob_t.c.id)
                )
            )
        ).all()
        if orphans:
            await conn.execute(sa.delete(blob_t).where(blob_t.c.id.in_([o.id for o in orphans])))
    for orphan in orphans:
        blobs.delete(orphan.sha256)
    return PurgeResult(k, m, e, len(orphans))
