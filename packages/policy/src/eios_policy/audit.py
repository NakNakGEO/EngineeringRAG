"""Append-only audit log of every policy decision."""

from __future__ import annotations

import uuid
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
