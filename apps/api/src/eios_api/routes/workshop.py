"""Capability gap resolution and the Capability Workshop.

Anyone may resolve a gap or *propose* an artifact. Registering a generated tool, verifying and
trusting need a human (admin token + named approver). Root Policy is not reachable from here.
"""

from __future__ import annotations

import hmac
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from eios_api.deps import ContainerDep
from eios_domain.errors import NotFoundError
from eios_domain.knowledge import HumanApproval, TrustViolationError
from eios_domain.registry import TransitionError
from eios_workshop import GapRequest, ProposalKind, ProposalState, WorkshopError

router = APIRouter(tags=["workshop"])
AdminToken = Annotated[str | None, Header(alias="X-EIOS-Admin-Token")]
_STATUS = {
    "blocked_by_policy": status.HTTP_403_FORBIDDEN,
    "approval_required": status.HTTP_401_UNAUTHORIZED,
    "invalid_spec": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "invalid_request": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "scan_failed": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "no_tests": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "not_enough_tests": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "verification_failed": status.HTTP_422_UNPROCESSABLE_CONTENT,
}


def _human(
    container: ContainerDep, token: str | None, approver: str | None, scope: str
) -> HumanApproval:
    configured = container.settings.admin_token
    if configured is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "human approvals are disabled (no admin token)"
        )
    if token is None or not hmac.compare_digest(
        token.encode(), configured.get_secret_value().encode()
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing admin token")
    if not approver or not approver.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "a named approver is required")
    return HumanApproval(approver=f"human:{approver.strip()}", scope=scope)


def _fail(exc: Exception) -> HTTPException:
    if isinstance(exc, WorkshopError):
        return HTTPException(_STATUS.get(exc.kind, status.HTTP_409_CONFLICT), str(exc))
    if isinstance(exc, NotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, TransitionError | TrustViolationError):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


@router.post("/gaps/resolve")
async def resolve_gap(body: GapRequest, container: ContainerDep) -> dict[str, Any]:
    """Existing -> compose -> generate skill/agent/tool -> external. Policy is checked first."""
    async with container.recorder.run(
        kind="gap_resolution", goal=(body.capability or body.description)[:200]
    ) as ctx:
        resolution = await container.resolver.resolve(body, ctx)
    return {"run_id": str(ctx.run_id), **resolution.model_dump(mode="json")}


class ProposalBody(BaseModel):
    kind: ProposalKind
    spec: dict[str, Any]
    source: str | None = Field(default=None, max_length=200_000)
    tests: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    need: dict[str, Any] = Field(default_factory=dict)
    new_capabilities: list[dict[str, Any]] = Field(default_factory=list, max_length=10)
    creator: str = Field(min_length=1, max_length=200)
    prompt: str | None = Field(default=None, max_length=100_000)


@router.post("/workshop/proposals", status_code=status.HTTP_201_CREATED)
async def create_proposal(body: ProposalBody, container: ContainerDep) -> dict[str, Any]:
    try:
        p = await container.workshop.create(
            kind=body.kind,
            spec=body.spec,
            creator=body.creator,
            creator_kind="llm",
            source=body.source,
            tests=body.tests,
            need=body.need,
            new_capabilities=body.new_capabilities,
            prompt=body.prompt,
        )
    except (WorkshopError, NotFoundError) as exc:
        raise _fail(exc) from exc
    return p.model_dump(mode="json")


@router.get("/workshop/proposals")
async def list_proposals(
    container: ContainerDep,
    state: ProposalState | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    items = await container.workshop.list_proposals(state, limit)
    return [
        {
            "id": str(p.id),
            "kind": p.kind.value,
            "name": p.name,
            "version": p.version,
            "state": p.state.value,
            "creator": p.creator,
            "created_at": p.created_at.isoformat(),
        }
        for p in items
    ]


@router.get("/workshop/proposals/{proposal_id}")
async def get_proposal(proposal_id: uuid.UUID, container: ContainerDep) -> dict[str, Any]:
    try:
        p = await container.workshop.get(proposal_id)
    except NotFoundError as exc:
        raise _fail(exc) from exc
    history = await container.workshop.history(proposal_id)
    return {
        **p.model_dump(mode="json"),
        "history": [
            {
                "from": h["from_state"],
                "to": h["to_state"],
                "actor": h["actor"],
                "reason": h["reason"],
                "at": h["at"].isoformat(),
            }
            for h in history
        ],
    }


@router.post("/workshop/proposals/{proposal_id}/sandbox")
async def sandbox_proposal(proposal_id: uuid.UUID, container: ContainerDep) -> dict[str, Any]:
    try:
        return (await container.workshop.sandbox(proposal_id, actor="api")).model_dump(mode="json")
    except (WorkshopError, NotFoundError) as exc:
        raise _fail(exc) from exc


@router.post("/workshop/proposals/{proposal_id}/test")
async def test_proposal(proposal_id: uuid.UUID, container: ContainerDep) -> dict[str, Any]:
    try:
        return (await container.workshop.test(proposal_id, actor="api")).model_dump(mode="json")
    except (WorkshopError, NotFoundError) as exc:
        raise _fail(exc) from exc


class HumanBody(BaseModel):
    approver: str | None = Field(default=None, max_length=200)
    reason: str = Field(default="", max_length=1000)


@router.post("/workshop/proposals/{proposal_id}/register")
async def register_proposal(
    proposal_id: uuid.UUID, body: HumanBody, container: ContainerDep, token: AdminToken = None
) -> dict[str, Any]:
    """Skills and agents register on their own once tested; tools need a human approval."""
    try:
        proposal = await container.workshop.get(proposal_id)
        human = (
            _human(container, token, body.approver, "workshop:register")
            if proposal.kind is ProposalKind.TOOL or token is not None
            else None
        )
        return (
            await container.workshop.register(proposal_id, actor="api", human=human)
        ).model_dump(mode="json")
    except (WorkshopError, NotFoundError, TransitionError) as exc:
        raise _fail(exc) from exc


@router.post("/workshop/proposals/{proposal_id}/verify")
async def verify_proposal(
    proposal_id: uuid.UUID, body: HumanBody, container: ContainerDep, token: AdminToken = None
) -> dict[str, Any]:
    human = _human(container, token, body.approver, "workshop:verify")
    try:
        return (await container.workshop.verify(proposal_id, human=human)).model_dump(mode="json")
    except (WorkshopError, NotFoundError, TransitionError) as exc:
        raise _fail(exc) from exc


@router.post("/workshop/proposals/{proposal_id}/trust")
async def trust_proposal(
    proposal_id: uuid.UUID, body: HumanBody, container: ContainerDep, token: AdminToken = None
) -> dict[str, Any]:
    human = _human(container, token, body.approver, "workshop:trust")
    try:
        return (await container.workshop.trust(proposal_id, human=human)).model_dump(mode="json")
    except (WorkshopError, NotFoundError, TransitionError) as exc:
        raise _fail(exc) from exc


@router.post("/workshop/proposals/{proposal_id}/reject")
async def reject_proposal(
    proposal_id: uuid.UUID, body: HumanBody, container: ContainerDep
) -> dict[str, Any]:
    try:
        return (
            await container.workshop.reject(
                proposal_id, actor=body.approver or "api", reason=body.reason
            )
        ).model_dump(mode="json")
    except (WorkshopError, NotFoundError, TransitionError) as exc:
        raise _fail(exc) from exc
