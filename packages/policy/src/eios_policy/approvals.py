"""Human approvals: requested by the engine, decided only by a human through the admin API."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Protocol

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.ids import new_id, utcnow
from eios_domain.policy import PolicyRequest
from eios_observability.redaction import redact
from eios_storage.tables.policy import approval as approval_t

DEFAULT_TTL = timedelta(hours=24)


class ApprovalError(Exception):
    """An approval operation that is not allowed (wrong state, expired, anonymous decider)."""


class Approval(BaseModel):
    id: uuid.UUID
    fingerprint: str
    action: str
    capability: str | None
    target: str | None
    requested_by: str
    run_id: uuid.UUID | None
    request: dict[str, Any]
    reason: str
    status: str
    decided_by: str | None
    decided_at: datetime | None
    decision_note: str
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None


def fingerprint(request: PolicyRequest) -> str:
    """Identity of *what* is being authorised: the same action on the same target by the same actor.

    An approval for one fingerprint can never be used for another.
    """
    canonical = json.dumps(
        {
            "action": request.action,
            "capability": request.capability,
            "target": request.target,
            "actor": f"{request.actor_type.value}:{request.actor_id}",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class ApprovalGate(Protocol):
    async def consume_approved(self, request: PolicyRequest) -> Approval | None: ...

    async def request_approval(self, request: PolicyRequest, reason: str) -> Approval: ...


class NoApprovals:
    """Gate that never has an approval (unit tests, dry runs)."""

    async def consume_approved(self, request: PolicyRequest) -> Approval | None:
        return None

    async def request_approval(self, request: PolicyRequest, reason: str) -> Approval:
        now = utcnow()
        return Approval(
            id=new_id(), fingerprint=fingerprint(request), action=request.action,
            capability=request.capability, target=request.target, requested_by=request.actor_id,
            run_id=request.run_id, request={}, reason=reason, status="pending", decided_by=None,
            decided_at=None, decision_note="", created_at=now, expires_at=now + DEFAULT_TTL,
            consumed_at=None,
        )  # fmt: skip


class PostgresApprovalStore:
    def __init__(self, engine: AsyncEngine, *, ttl: timedelta = DEFAULT_TTL) -> None:
        self._engine = engine
        self._ttl = ttl

    async def request_approval(self, request: PolicyRequest, reason: str) -> Approval:
        """Create a pending approval, or return the one already pending for the same action."""
        fp = fingerprint(request)
        now = utcnow()
        async with self._engine.begin() as conn:
            existing = (
                await conn.execute(
                    sa.select(approval_t).where(
                        approval_t.c.fingerprint == fp,
                        approval_t.c.status == "pending",
                        approval_t.c.expires_at > now,
                    )
                )
            ).first()
            if existing is not None:
                return Approval(**dict(existing._mapping))
            row = {
                "id": new_id(),
                "fingerprint": fp,
                "action": request.action,
                "capability": request.capability,
                "target": request.target,
                "requested_by": f"{request.actor_type.value}:{request.actor_id}",
                "run_id": request.run_id,
                "request": redact(request.model_dump(mode="json")),
                "reason": reason[:500],
                "status": "pending",
                "created_at": now,
                "expires_at": now + self._ttl,
            }
            await conn.execute(sa.insert(approval_t).values(**row))
        return await self.get(row["id"])  # type: ignore[return-value]

    async def get(self, approval_id: uuid.UUID) -> Approval | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(sa.select(approval_t).where(approval_t.c.id == approval_id))
            ).first()
        return Approval(**dict(row._mapping)) if row else None

    async def list(self, *, status: str | None = None, limit: int = 100) -> list[Approval]:
        stmt = sa.select(approval_t).order_by(approval_t.c.created_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(approval_t.c.status == status)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [Approval(**dict(r._mapping)) for r in rows]

    async def decide(
        self, approval_id: uuid.UUID, *, approve: bool, decided_by: str, note: str = ""
    ) -> Approval:
        """Record a human decision. Only a pending, unexpired approval can be decided."""
        if not decided_by.strip():
            raise ApprovalError("a decision needs a named decider")
        now = utcnow()
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    sa.select(approval_t).where(approval_t.c.id == approval_id).with_for_update()
                )
            ).first()
            if row is None:
                raise LookupError(f"approval {approval_id} not found")
            current = row._mapping
            if current["status"] != "pending":
                raise ApprovalError(f"approval is already {current['status']}")
            if current["expires_at"] <= now:
                await conn.execute(
                    sa.update(approval_t)
                    .where(approval_t.c.id == approval_id)
                    .values(status="expired")
                )
                raise ApprovalError("approval request has expired")
            await conn.execute(
                sa.update(approval_t)
                .where(approval_t.c.id == approval_id)
                .values(
                    status="approved" if approve else "denied",
                    decided_by=decided_by[:200],
                    decided_at=now,
                    decision_note=note[:1000],
                )
            )
        return await self.get(approval_id)  # type: ignore[return-value]

    async def consume_approved(self, request: PolicyRequest) -> Approval | None:
        """Atomically use up one matching approved, unexpired approval (single use)."""
        now = utcnow()
        fp = fingerprint(request)
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    sa.select(approval_t.c.id)
                    .where(
                        approval_t.c.fingerprint == fp,
                        approval_t.c.status == "approved",
                        approval_t.c.expires_at > now,
                    )
                    .order_by(approval_t.c.decided_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).first()
            if row is None:
                return None
            await conn.execute(
                sa.update(approval_t)
                .where(approval_t.c.id == row.id)
                .values(status="consumed", consumed_at=now)
            )
        return await self.get(row.id)

    async def expire_old(self) -> int:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                sa.update(approval_t)
                .where(
                    approval_t.c.status.in_(["pending", "approved"]),
                    approval_t.c.expires_at <= utcnow(),
                )
                .values(status="expired")
            )
        return result.rowcount or 0
