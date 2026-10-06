"""Knowledge governance, the decision ledger, evaluation and maintenance.

Anyone may *propose*; promotion to APPROVED/ACCEPTED and other human-only acts need the admin
token (``X-EIOS-Admin-Token``) and a named approver. A model-level request can never raise trust
without independent evidence.
"""

from __future__ import annotations

import hmac
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from eios_api.deps import ContainerDep
from eios_domain.errors import DomainError, NotFoundError
from eios_domain.knowledge import (
    DecisionCreate,
    DecisionStatus,
    Health,
    HumanApproval,
    Trust,
    TrustViolationError,
)
from eios_domain.vault import Vault
from eios_governance.evaluation import load_suites
from eios_governance.maintenance import MAINTAIN_JOB

router = APIRouter(tags=["governance"])
AdminToken = Annotated[str | None, Header(alias="X-EIOS-Admin-Token")]


def _human(
    container: ContainerDep, token: str | None, approver: str | None, scope: str
) -> HumanApproval | None:
    """A HumanApproval exists only when the admin token is valid and a named approver is given."""
    if token is None and approver is None:
        return None
    configured = container.settings.admin_token
    if configured is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "human approvals are disabled (no admin token)"
        )
    if token is None or not hmac.compare_digest(
        token.encode(), configured.get_secret_value().encode()
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid admin token")
    if not approver or not approver.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "a named approver is required")
    return HumanApproval(approver=f"human:{approver.strip()}", scope=scope)


def _require_human(
    container: ContainerDep, token: str | None, approver: str | None, scope: str
) -> HumanApproval:
    human = _human(container, token, approver, scope)
    if human is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "this action needs a human (admin token + approver)"
        )
    return human


def _errors(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError | LookupError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, TrustViolationError):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


# ---- knowledge trust / health ----
class TrustBody(BaseModel):
    target: Trust
    reason: str = Field(default="", max_length=1000)
    evidence_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    actor: str = Field(default="api", max_length=200)
    approver: str | None = None


@router.post("/governance/items/{item_id}/trust")
async def change_trust(
    item_id: uuid.UUID, body: TrustBody, container: ContainerDep, token: AdminToken = None
) -> dict[str, Any]:
    human = _human(container, token, body.approver, f"trust:{body.target.value}")
    try:
        item = await container.governance.promote_trust(
            item_id,
            body.target,
            actor=human.approver if human else body.actor,
            reason=body.reason,
            evidence_ids=body.evidence_ids,
            human=human,
        )
    except (DomainError, NotFoundError, TrustViolationError) as exc:
        raise _errors(exc) from exc
    return {"id": str(item.id), "trust": item.trust.value, "health": item.health.value}


class HealthBody(BaseModel):
    target: Health
    reason: str = Field(default="", max_length=1000)
    actor: str = Field(default="api", max_length=200)
    approver: str | None = None


@router.post("/governance/items/{item_id}/health")
async def change_health(
    item_id: uuid.UUID, body: HealthBody, container: ContainerDep, token: AdminToken = None
) -> dict[str, Any]:
    human = _human(container, token, body.approver, f"health:{body.target.value}")
    try:
        item = await container.governance.set_health(
            item_id,
            body.target,
            actor=human.approver if human else body.actor,
            reason=body.reason,
            human=human,
        )
    except (DomainError, NotFoundError, TrustViolationError) as exc:
        raise _errors(exc) from exc
    return {"id": str(item.id), "trust": item.trust.value, "health": item.health.value}


class SupersedeBody(BaseModel):
    old_id: uuid.UUID
    new_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=1000)
    actor: str = Field(default="api", max_length=200)
    approver: str | None = None


@router.post("/governance/supersede")
async def supersede_item(
    body: SupersedeBody, container: ContainerDep, token: AdminToken = None
) -> dict[str, Any]:
    human = _human(container, token, body.approver, "supersede")
    try:
        item = await container.governance.supersede(
            body.old_id,
            body.new_id,
            actor=human.approver if human else body.actor,
            reason=body.reason,
            human=human,
        )
    except (DomainError, NotFoundError, TrustViolationError) as exc:
        raise _errors(exc) from exc
    return {
        "id": str(item.id),
        "health": item.health.value,
        "superseded_by": str(item.superseded_by),
    }


@router.get("/governance/items/{item_id}/lifecycle")
async def item_lifecycle(item_id: uuid.UUID, container: ContainerDep) -> list[dict[str, Any]]:
    rows = await container.governance.store.history("item", item_id)
    return [
        {
            k: (
                str(v)
                if isinstance(v, uuid.UUID)
                else v.isoformat()
                if hasattr(v, "isoformat")
                else v
            )
            for k, v in r.items()
        }
        for r in rows
    ]


class DependenciesBody(BaseModel):
    files: list[dict[str, str | None]] = Field(default_factory=list, max_length=500)
    symbols: list[str] = Field(default_factory=list, max_length=500)
    items: list[uuid.UUID] = Field(default_factory=list, max_length=500)


@router.put("/governance/items/{item_id}/dependencies")
async def set_dependencies(
    item_id: uuid.UUID, body: DependenciesBody, container: ContainerDep
) -> dict[str, int]:
    try:
        files = [(str(f["path"]), f.get("hash")) for f in body.files if f.get("path")]
        count = await container.governance.link_dependencies(
            item_id, files=files, symbols=body.symbols, items=body.items
        )
    except (DomainError, NotFoundError) as exc:
        raise _errors(exc) from exc
    return {"dependencies": count}


# ---- contradictions ----
class ContradictionBody(BaseModel):
    item_a: uuid.UUID
    item_b: uuid.UUID
    reason: str = Field(min_length=1, max_length=1000)
    actor: str = Field(default="api", max_length=200)


@router.post("/governance/contradictions", status_code=status.HTTP_201_CREATED)
async def report_contradiction(body: ContradictionBody, container: ContainerDep) -> dict[str, Any]:
    try:
        cid = await container.governance.report_contradiction(
            body.item_a, body.item_b, reason=body.reason, actor=body.actor
        )
    except (DomainError, NotFoundError) as exc:
        raise _errors(exc) from exc
    return {"contradiction_id": str(cid) if cid else None, "already_known": cid is None}


@router.get("/governance/contradictions")
async def list_contradictions(
    container: ContainerDep, status_filter: Annotated[str | None, Query(alias="status")] = "open"
) -> list[dict[str, Any]]:
    rows = await container.governance.store.contradictions(status=status_filter)
    return [
        {
            k: (
                str(v)
                if isinstance(v, uuid.UUID)
                else v.isoformat()
                if hasattr(v, "isoformat")
                else v
            )
            for k, v in r.items()
        }
        for r in rows
    ]


class ResolveBody(BaseModel):
    winner: uuid.UUID
    resolution: str = Field(min_length=1, max_length=1000)
    approver: str = Field(min_length=1, max_length=200)


@router.post("/governance/contradictions/{contradiction_id}/resolve")
async def resolve_contradiction(
    contradiction_id: uuid.UUID,
    body: ResolveBody,
    container: ContainerDep,
    token: AdminToken = None,
) -> dict[str, str]:
    human = _require_human(container, token, body.approver, "contradiction")
    try:
        await container.governance.resolve_contradiction(
            contradiction_id, winner=body.winner, human=human, resolution=body.resolution
        )
    except (DomainError, NotFoundError, TrustViolationError) as exc:
        raise _errors(exc) from exc
    return {"status": "resolved"}


# ---- decision ledger ----
class ProposeDecision(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    selected: str = Field(default="", max_length=2000)
    rationale: str = Field(default="", max_length=20_000)
    alternatives: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    evidence_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    project_id: uuid.UUID | None = None
    proposer: str = Field(default="api", max_length=200)


@router.post("/decisions", status_code=status.HTTP_201_CREATED)
async def propose_decision(body: ProposeDecision, container: ContainerDep) -> dict[str, Any]:
    """Always recorded as PROPOSED: acceptance is a human act."""
    try:
        decision = await container.knowledge.record_decision(
            DecisionCreate(
                vault=Vault.PROJECT if body.project_id else Vault.DEFAULT,
                project_id=body.project_id,
                question=body.question,
                selected=body.selected,
                rationale=body.rationale,
                alternatives=body.alternatives,
                evidence_ids=body.evidence_ids,
                decider=body.proposer,
                status=DecisionStatus.PROPOSED,
            )
        )
    except (DomainError, ValueError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return decision.model_dump(mode="json")


@router.get("/decisions/{decision_id}")
async def get_decision(decision_id: uuid.UUID, container: ContainerDep) -> dict[str, Any]:
    decision = await container.knowledge.decisions.get(decision_id)
    if decision is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "decision not found")
    history = await container.decisions.history(decision_id)
    return {
        **decision.model_dump(mode="json"),
        "history": [
            {
                "change": h["change"],
                "from": h["from_value"],
                "to": h["to_value"],
                "actor": h["actor"],
                "reason": h["reason"],
                "at": h["at"].isoformat(),
            }
            for h in history
        ],
    }


class DecisionAct(BaseModel):
    approver: str = Field(min_length=1, max_length=200)
    reason: str = Field(default="", max_length=1000)
    superseded_by: uuid.UUID | None = None


@router.post("/decisions/{decision_id}/{act}")
async def decide(
    decision_id: uuid.UUID,
    act: str,
    body: DecisionAct,
    container: ContainerDep,
    token: AdminToken = None,
) -> dict[str, Any]:
    if act not in {"accept", "reject", "supersede"}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown action")
    human = _require_human(container, token, body.approver, f"decision:{act}")
    try:
        if act == "accept":
            d = await container.decisions.accept(decision_id, human, body.reason)
        elif act == "reject":
            d = await container.decisions.reject(decision_id, human, body.reason)
        else:
            if body.superseded_by is None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT, "superseded_by is required"
                )
            d = await container.decisions.supersede(
                decision_id, body.superseded_by, human, body.reason
            )
    except (DomainError, NotFoundError) as exc:
        raise _errors(exc) from exc
    return d.model_dump(mode="json")


# ---- evaluation & maintenance ----
class EvalBody(BaseModel):
    provider: str
    promote: bool = False


@router.get("/evaluations/suites")
async def list_suites(container: ContainerDep) -> list[dict[str, Any]]:
    suites, _ = load_suites(container.settings.evals_dir)
    return [
        {
            "provider": s.provider,
            "capability": s.capability,
            "cases": len(s.cases),
            "min_pass_rate": s.min_pass_rate,
        }
        for s in suites
    ]


@router.post("/evaluations/run")
async def run_evaluation(body: EvalBody, container: ContainerDep) -> dict[str, Any]:
    suites, _ = load_suites(container.settings.evals_dir)
    matching = [s for s in suites if s.provider == body.provider]
    if not matching:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no evaluation suite for '{body.provider}'")
    try:
        async with container.recorder.run(
            kind="evaluation", goal=f"evaluate {body.provider}"
        ) as ctx:
            reports = [
                await container.evaluation.run_suite(s, promote=body.promote, ctx=ctx)
                for s in matching
            ]
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"run_id": str(ctx.run_id), "reports": [r.model_dump(mode="json") for r in reports]}


class MaintenanceBody(BaseModel):
    project_id: uuid.UUID | None = None
    wait: bool = False


@router.post("/governance/maintenance", status_code=status.HTTP_202_ACCEPTED)
async def run_maintenance(body: MaintenanceBody, container: ContainerDep) -> dict[str, Any]:
    if body.wait:
        async with container.recorder.run(kind="maintenance", goal="knowledge maintenance") as ctx:
            report = await container.maintenance.run(body.project_id, ctx)
        return {"run_id": str(ctx.run_id), **report.__dict__}
    job = await container.queue.enqueue(
        MAINTAIN_JOB, {"project_id": str(body.project_id) if body.project_id else None}
    )
    return {"job_id": str(job.id)}
