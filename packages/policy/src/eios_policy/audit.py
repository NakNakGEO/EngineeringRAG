"""Append-only audit log of every policy decision."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Protocol

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.ids import new_id, utcnow
from eios_domain.policy import PolicyDecision
from eios_observability.redaction import redact
from eios_storage.tables.policy import audit_log as audit_t


class AuditEntry(BaseModel):
    id: uuid.UUID
    at: datetime
    actor_type: str
    actor_id: str
    action: str
    capability: str | None
    target: str | None
    effect: str
    rule_id: str
    risk: str
    reasons: list[str]
    run_id: uuid.UUID | None
    root_policy_version: str | None
    detail: dict[str, Any]


class AuditSink(Protocol):
    async def record(self, decision: PolicyDecision) -> None: ...


class NullAudit:
    def __init__(self) -> None:
        self.decisions: list[PolicyDecision] = []

    async def record(self, decision: PolicyDecision) -> None:
        self.decisions.append(decision)


class PostgresAuditLog:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(self, decision: PolicyDecision) -> None:
        req = decision.request
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.insert(audit_t).values(
                    id=new_id(),
                    at=utcnow(),
                    actor_type=req.actor_type.value,
                    actor_id=req.actor_id[:200],
                    action=req.action[:200],
                    capability=req.capability,
                    target=(req.target or "")[:1000] or None,
                    effect=decision.effect.value,
                    rule_id=decision.rule_id,
                    risk=decision.risk.value,
                    reasons=decision.reasons,
                    run_id=req.run_id,
                    root_policy_version=decision.root_policy_version,
                    detail=redact(
                        {
                            "decision_id": str(decision.decision_id),
                            "approval_id": str(decision.approval_id)
                            if decision.approval_id
                            else None,
                            "attributes": req.attributes,
                        }
                    ),
                )
            )

    async def list(
        self, *, effect: str | None = None, run_id: uuid.UUID | None = None, limit: int = 100
    ) -> list[AuditEntry]:
        stmt = sa.select(audit_t).order_by(audit_t.c.at.desc()).limit(limit)
        if effect:
            stmt = stmt.where(audit_t.c.effect == effect)
        if run_id:
            stmt = stmt.where(audit_t.c.run_id == run_id)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [AuditEntry(**dict(r._mapping)) for r in rows]


GENESIS = "0" * 64


def chain_hash(previous: str, record: dict[str, Any]) -> str:
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((previous + canonical).encode()).hexdigest()


async def export_ndjson(
    engine: AsyncEngine, *, since: datetime | None = None, batch: int = 500
) -> AsyncIterator[str]:
    """Stream the audit log as NDJSON, hash-chained so any later edit or removal is detectable.

    Each line carries ``prev`` and ``hash`` (sha256 over prev + the record); the last line is an
    ``end`` marker with the entry count and the head hash.
    """
    previous, count = GENESIS, 0
    cursor: tuple[datetime, uuid.UUID] | None = None
    while True:
        stmt = sa.select(audit_t).order_by(audit_t.c.at, audit_t.c.id).limit(batch)
        if since is not None:
            stmt = stmt.where(audit_t.c.at >= since)
        if cursor is not None:
            stmt = stmt.where(sa.tuple_(audit_t.c.at, audit_t.c.id) > sa.tuple_(*cursor))
        async with engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        if not rows:
            break
        for row in rows:
            record = {
                k: (
                    str(v)
                    if isinstance(v, uuid.UUID)
                    else v.isoformat()
                    if isinstance(v, datetime)
                    else v
                )
                for k, v in dict(row._mapping).items()
            }
            digest = chain_hash(previous, record)
            yield (
                json.dumps(
                    {"type": "audit", "prev": previous, "hash": digest, "record": record},
                    sort_keys=True,
                )
                + "\n"
            )
            previous, count = digest, count + 1
        cursor = (rows[-1]._mapping["at"], rows[-1]._mapping["id"])
    yield json.dumps({"type": "end", "count": count, "head": previous}) + "\n"


def verify_ndjson(lines: list[str]) -> tuple[bool, str]:
    """Check chain integrity of an exported audit log."""
    previous, count = GENESIS, 0
    for number, line in enumerate(lines, 1):
        entry = json.loads(line)
        if entry["type"] == "end":
            ok = entry["count"] == count and entry["head"] == previous and number == len(lines)
            return ok, "ok" if ok else "end marker does not match the chain"
        if entry["prev"] != previous or entry["hash"] != chain_hash(previous, entry["record"]):
            return False, f"chain broken at line {number}"
        previous, count = entry["hash"], count + 1
    return False, "missing end marker (truncated export)"
