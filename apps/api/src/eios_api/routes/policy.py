"""Policy, approvals and audit.

Root Policy is **read-only** here: there is no endpoint that writes it. Approvals can be decided
only by a human presenting the admin token; the MCP/LLM surface never exposes this router.
"""

from __future__ import annotations

import hmac
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from eios_api.deps import ContainerDep
from eios_domain.events import ActorType
from eios_domain.policy import PolicyRequest
from eios_policy import ApprovalError

router = APIRouter(tags=["policy"])


class EvaluateRequest(BaseModel):
    actor_type: ActorType = ActorType.LLM
    actor_id: str = Field(default="api", max_length=200)
    action: str = Field(min_length=1, max_length=100)
    capability: str | None = Field(default=None, max_length=100)
    target: str | None = Field(default=None, max_length=1000)
    attributes: dict[str, Any] = Field(default_factory=dict)


class DecisionBody(BaseModel):
    approve: bool
    decided_by: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=1000)


def _require_admin(container: ContainerDep, token: str | None) -> None:
    configured = container.settings.admin_token
    if configured is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "approvals are disabled: EIOS_ADMIN_TOKEN is not configured"
        )
    if token is None or not hmac.compare_digest(
        token.encode(), configured.get_secret_value().encode()
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid admin token")


@router.get("/policy/root")
async def root_policy(container: ContainerDep) -> dict[str, object]:
    """Read-only view of the active Root Policy (version, digest, key rules)."""
    return container.policy.root.summary()


@router.post("/policy/evaluate")
async def evaluate(body: EvaluateRequest, container: ContainerDep) -> dict[str, Any]:
    """Dry-run a policy question. Nothing is executed, approved, consumed or audited."""
    decision = await container.policy.evaluate(PolicyRequest(**body.model_dump()), dry_run=True)
    return {
        "effect": decision.effect.value,
        "rule_id": decision.rule_id,
        "reasons": decision.reasons,
        "risk": decision.risk.value,
        "root_policy_version": decision.root_policy_version,
    }


@router.get("/approvals")
async def list_approvals(
    container: ContainerDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict[str, Any]]:
    items = await container.approvals.list(status=status_filter, limit=limit)
    return [a.model_dump(mode="json") for a in items]


@router.get("/approvals/{approval_id}")
async def get_approval(approval_id: uuid.UUID, container: ContainerDep) -> dict[str, Any]:
    approval = await container.approvals.get(approval_id)
    if approval is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found")
    return approval.model_dump(mode="json")


@router.post("/approvals/{approval_id}/decision")
async def decide_approval(
    approval_id: uuid.UUID,
    body: DecisionBody,
    container: ContainerDep,
    x_eios_admin_token: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Human decision. Requires ``X-EIOS-Admin-Token``; an LLM or tool has no way to call this."""
    _require_admin(container, x_eios_admin_token)
    try:
        approval = await container.approvals.decide(
            approval_id, approve=body.approve, decided_by=f"human:{body.decided_by}", note=body.note
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ApprovalError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return approval.model_dump(mode="json")


@router.get("/audit")
async def audit_log(
    container: ContainerDep,
    effect: Annotated[str | None, Query(pattern="^(allow|deny|require_approval)$")] = None,
    run_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict[str, Any]]:
    entries = await container.audit.list(effect=effect, run_id=run_id, limit=limit)
    return [e.model_dump(mode="json") for e in entries]
