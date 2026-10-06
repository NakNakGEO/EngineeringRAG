"""Retention: bounded storage without ever bypassing the append-only guarantees casually.

Deleting from append-only tables (events, audit) is only possible inside a transaction that sets
``eios.retention = 'on'`` - something only this job does. Audit retention is off by default
(keep forever) and should be preceded by an audit export.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.ids import utcnow
from eios_knowledge.blob_store import BlobStore
from eios_storage.tables.evidence import blob as blob_t
from eios_storage.tables.evidence import record as record_t
from eios_storage.tables.knowledge import item as item_t
from eios_storage.tables.memory import item as memory_t
from eios_storage.tables.observability import event as event_t
from eios_storage.tables.platform import job as job_t
from eios_storage.tables.platform import run as run_t
from eios_storage.tables.policy import approval as approval_t
from eios_storage.tables.policy import audit_log as audit_t

RETENTION_JOB = "platform.retention"


@dataclass
class RetentionPolicy:
    event_days: int | None = 90  # events of finished runs
    audit_days: int | None = None  # None = keep forever
    job_days: int | None = 14  # finished/dead jobs
    approval_days: int | None = 365  # decided/expired approvals


@dataclass
class RetentionReport:
    deleted: dict[str, int] = field(default_factory=dict)


class RetentionService:
    def __init__(
        self, engine: AsyncEngine, blobs: BlobStore | None, policy: RetentionPolicy | None = None
    ) -> None:
        self._engine = engine
        self._blobs = blobs
        self._policy = policy or RetentionPolicy()

    async def run(self) -> RetentionReport:
        report = RetentionReport()
        now = utcnow()
        async with self._engine.begin() as conn:
            report.deleted["ephemeral_knowledge"] = (
                await conn.execute(sa.delete(item_t).where(item_t.c.expires_at < now))
            ).rowcount or 0
            report.deleted["expired_memory"] = (
                await conn.execute(sa.delete(memory_t).where(memory_t.c.expires_at < now))
            ).rowcount or 0
            expired = (
                (await conn.execute(sa.select(record_t.c.id).where(record_t.c.expires_at < now)))
                .scalars()
                .all()
            )
            if expired:
                await conn.execute(sa.delete(record_t).where(record_t.c.id.in_(expired)))
            report.deleted["expired_evidence"] = len(expired)
            orphans = (
                await conn.execute(
                    sa.select(blob_t.c.id, blob_t.c.sha256).where(
                        ~sa.exists().where(record_t.c.blob_id == blob_t.c.id)
                    )
                )
            ).all()
            if orphans:
                await conn.execute(
                    sa.delete(blob_t).where(blob_t.c.id.in_([o.id for o in orphans]))
                )
            report.deleted["orphan_blobs"] = len(orphans)
            await conn.execute(sa.text("SELECT set_config('eios.retention', 'on', true)"))
            if self._policy.event_days is not None:
                cutoff = now - timedelta(days=self._policy.event_days)
                finished = sa.select(run_t.c.id).where(
                    run_t.c.finished_at.is_not(None), run_t.c.finished_at < cutoff
                )
                report.deleted["events"] = (
                    await conn.execute(sa.delete(event_t).where(event_t.c.run_id.in_(finished)))
                ).rowcount or 0
            if self._policy.audit_days is not None:
                cutoff = now - timedelta(days=self._policy.audit_days)
                report.deleted["audit"] = (
                    await conn.execute(sa.delete(audit_t).where(audit_t.c.at < cutoff))
                ).rowcount or 0
            if self._policy.job_days is not None:
                cutoff = now - timedelta(days=self._policy.job_days)
                report.deleted["jobs"] = (
                    await conn.execute(
                        sa.delete(job_t).where(
                            job_t.c.status.in_(["succeeded", "dead"]), job_t.c.updated_at < cutoff
                        )
                    )
                ).rowcount or 0
            await conn.execute(
                sa.update(approval_t)
                .where(
                    approval_t.c.status.in_(["pending", "approved"]), approval_t.c.expires_at <= now
                )
                .values(status="expired")
            )
            if self._policy.approval_days is not None:
                cutoff = now - timedelta(days=self._policy.approval_days)
                report.deleted["approvals"] = (
                    await conn.execute(
                        sa.delete(approval_t).where(
                            approval_t.c.status.in_(["denied", "expired", "consumed"]),
                            approval_t.c.created_at < cutoff,
                        )
                    )
                ).rowcount or 0
        if self._blobs is not None:
            for sha in {o.sha256 for o in orphans}:
                if not await self._still_referenced(sha):
                    self._blobs.delete(sha)
        return report

    async def _still_referenced(self, sha: str) -> bool:
        async with self._engine.connect() as conn:
            return (
                await conn.execute(sa.select(blob_t.c.id).where(blob_t.c.sha256 == sha))
            ).first() is not None
