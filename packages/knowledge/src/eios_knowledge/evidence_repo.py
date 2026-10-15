"""Evidence, observation and finding persistence."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.errors import require_found
from eios_domain.ids import new_id, utcnow
from eios_domain.knowledge import (
    EvidenceCreate,
    EvidenceRecord,
    Finding,
    Observation,
    SourceKind,
    Trust,
)
from eios_domain.vault import Vault
from eios_knowledge.blob_store import BlobRef
from eios_storage.tables.evidence import blob as blob_t
from eios_storage.tables.evidence import finding as finding_t
from eios_storage.tables.evidence import observation as observation_t
from eios_storage.tables.evidence import record as record_t


def _record(row: Any) -> EvidenceRecord:
    m = row._mapping
    return EvidenceRecord(
        id=m["id"],
        vault=Vault(m["vault"]),
        project_id=m["project_id"],
        run_id=m["run_id"],
        source_kind=SourceKind(m["source_kind"]),
        tool_id=m["tool_id"],
        summary=m["summary"],
        media_type=m["media_type"],
        content_hash=m["content_hash"],
        size_bytes=m["size_bytes"],
        blob_id=m["blob_id"],
        created_at=m["created_at"],
        expires_at=m["expires_at"],
        trust=Trust(m["trust"]),
    )


class EvidenceRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create_record(self, create: EvidenceCreate, blob: BlobRef) -> EvidenceRecord:
        record_id, blob_id, now = new_id(), new_id(), utcnow()
        async with self._engine.begin() as conn:
            existing = (
                await conn.execute(sa.select(blob_t.c.id).where(blob_t.c.sha256 == blob.sha256))
            ).scalar_one_or_none()
            if existing is None:
                await conn.execute(
                    sa.insert(blob_t).values(
                        id=blob_id,
                        sha256=blob.sha256,
                        size_bytes=blob.size_bytes,
                        storage_path=blob.storage_path,
                        media_type=create.media_type,
                        created_at=now,
                    )
                )
            await conn.execute(
                sa.insert(record_t).values(
                    id=record_id,
                    vault=create.vault.value,
                    project_id=create.project_id,
                    run_id=create.run_id,
                    source_kind=create.source_kind.value,
                    tool_id=create.tool_id,
                    summary=create.summary,
                    media_type=create.media_type,
                    content_hash=blob.sha256,
                    size_bytes=blob.size_bytes,
                    blob_id=existing or blob_id,
                    trust=Trust.RAW.value,
                    created_at=now,
                    expires_at=create.expires_at,
                    metadata=create.metadata,
                )
            )
        return require_found(await self.get(record_id), "record")

    async def get(self, evidence_id: uuid.UUID) -> EvidenceRecord | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(sa.select(record_t).where(record_t.c.id == evidence_id))
            ).first()
        return _record(row) if row else None

    async def get_many(self, ids: list[uuid.UUID]) -> list[EvidenceRecord]:
        if not ids:
            return []
        async with self._engine.connect() as conn:
            rows = (await conn.execute(sa.select(record_t).where(record_t.c.id.in_(ids)))).all()
        return [_record(r) for r in rows]

    async def blob_sha(self, evidence_id: uuid.UUID) -> str | None:
        async with self._engine.connect() as conn:
            return (
                await conn.execute(
                    sa.select(blob_t.c.sha256)
                    .select_from(record_t.join(blob_t, record_t.c.blob_id == blob_t.c.id))
                    .where(record_t.c.id == evidence_id)
                )
            ).scalar_one_or_none()

    async def add_observation(
        self, evidence_id: uuid.UUID, statement: str, confidence: float, created_by: str
    ) -> Observation:
        obs_id, now = new_id(), utcnow()
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.insert(observation_t).values(
                    id=obs_id,
                    evidence_id=evidence_id,
                    statement=statement,
                    confidence=confidence,
                    created_by=created_by,
                    created_at=now,
                )
            )
        return Observation(
            id=obs_id,
            evidence_id=evidence_id,
            statement=statement,
            confidence=confidence,
            created_by=created_by,
            created_at=now,
        )

    async def get_observations(self, ids: list[uuid.UUID]) -> list[Observation]:
        if not ids:
            return []
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(sa.select(observation_t).where(observation_t.c.id.in_(ids)))
            ).all()
        return [Observation(**dict(r._mapping)) for r in rows]

    async def add_finding(
        self,
        observation_ids: list[uuid.UUID],
        statement: str,
        confidence: float,
        limitations: str,
        created_by: str,
    ) -> Finding:
        finding_id, now = new_id(), utcnow()
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.insert(finding_t).values(
                    id=finding_id,
                    observation_ids=observation_ids,
                    statement=statement,
                    confidence=confidence,
                    limitations=limitations,
                    created_by=created_by,
                    created_at=now,
                )
            )
        return Finding(
            id=finding_id,
            observation_ids=observation_ids,
            statement=statement,
            confidence=confidence,
            limitations=limitations,
            created_by=created_by,
            created_at=now,
        )

    async def get_finding(self, finding_id: uuid.UUID) -> Finding | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(sa.select(finding_t).where(finding_t.c.id == finding_id))
            ).first()
        return Finding(**dict(row._mapping)) if row else None

    async def expired_ids(self, now: datetime) -> list[uuid.UUID]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.select(record_t.c.id).where(
                        record_t.c.expires_at.is_not(None), record_t.c.expires_at <= now
                    )
                )
            ).all()
        return [r[0] for r in rows]
