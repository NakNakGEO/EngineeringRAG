"""Workflow graph and agent team selection."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from eios_api.deps import ContainerDep
from eios_domain.policy import Risk
from eios_domain.workflow import WorkflowRun, WorkflowStatus
from eios_workflow import TeamSignals, WorkflowError, select_team

router = APIRouter(tags=["workflows"])

_STATUS_FOR_KIND = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "unknown_definition": status.HTTP_404_NOT_FOUND,
    "conflict": status.HTTP_409_CONFLICT,
    "invalid_state": status.HTTP_409_CONFLICT,
    "waiting_approval": status.HTTP_409_CONFLICT,
    "wrong_node": status.HTTP_409_CONFLICT,
    "writer_violation": status.HTTP_403_FORBIDDEN,
    "unknown_role": status.HTTP_422_UNPROCESSABLE_CONTENT,
}


def _http(exc: WorkflowError) -> HTTPException:
    return HTTPException(_STATUS_FOR_KIND.get(exc.kind, status.HTTP_400_BAD_REQUEST), str(exc))


class Signals(BaseModel):
    paths: list[str] = Field(default_factory=list, max_length=100)
    languages: list[str] = Field(default_factory=list, max_length=20)
    dependents: int = Field(default=0, ge=0)
    tests_missing: bool = False
    touches_sql: bool = False
    coverage: float | None = Field(default=None, ge=0, le=1)
    explicit_targets: bool = False

    def to_signals(self, risk: Risk) -> TeamSignals:
        return TeamSignals(
            risk=risk, paths=tuple(self.paths), languages=frozenset(self.languages),
            dependents=self.dependents, tests_missing=self.tests_missing,
            touches_sql=self.touches_sql, coverage=self.coverage,
            explicit_targets=self.explicit_targets,
        )  # fmt: skip


class TeamRequest(BaseModel):
    goal: str = Field(min_length=3, max_length=4000)
    risk: Risk = Risk.MEDIUM
    signals: Signals = Field(default_factory=Signals)


class StartRequest(TeamRequest):
    definition_id: str = "engineering.default"
    project_id: uuid.UUID | None = None


class ReportRequest(BaseModel):
    node_id: str = Field(min_length=1, max_length=60)
    reporter: str = Field(min_length=1, max_length=80)
    outputs: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)


class CancelRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)


@router.post("/team/select")
async def preview_team(body: TeamRequest, container: ContainerDep) -> dict[str, Any]:
    """The minimum effective team for a goal, with reasons and the roles that were NOT used."""
    from eios_runtime.container import _agent_infos

    agents = await _agent_infos(container.registry.store)
    team = select_team(body.goal, body.signals.to_signals(body.risk), agents)
    return team.model_dump(mode="json")


@router.get("/workflow-definitions")
async def definitions(container: ContainerDep) -> list[dict[str, Any]]:
    return [
        {
            "id": d.id, "version": d.version, "description": d.description,
            "nodes": [{"id": n.id, "stage": n.stage.value, "next": n.next} for n in d.nodes],
        }
        for d in container.workflows.definitions.values()
    ]  # fmt: skip


@router.post("/workflows", status_code=status.HTTP_201_CREATED)
async def start_workflow(body: StartRequest, container: ContainerDep) -> dict[str, Any]:
    try:
        run = await container.workflows.start(
            goal=body.goal, definition_id=body.definition_id, project_id=body.project_id,
            risk=body.risk, signals=body.signals.to_signals(body.risk),
        )  # fmt: skip
    except WorkflowError as exc:
        raise _http(exc) from exc
    return await container.workflows.brief(run.id)


@router.get("/workflows")
async def list_workflows(
    container: ContainerDep,
    status_filter: Annotated[WorkflowStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    runs = await container.workflows.list_runs(status=status_filter, limit=limit)
    return [
        {
            "id": str(r.id), "run_id": str(r.run_id), "goal": r.goal[:200],
            "status": r.status.value, "current_node": r.current_node, "risk": r.risk.value,
            "primary_role": r.team.primary_role, "created_at": r.created_at.isoformat(),
        }
        for r in runs
    ]  # fmt: skip


@router.get("/workflows/{workflow_id}")
async def get_workflow(workflow_id: uuid.UUID, container: ContainerDep) -> WorkflowRun:
    run = await container.workflows.get(workflow_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"workflow {workflow_id} not found")
    return run


@router.get("/workflows/{workflow_id}/brief")
async def workflow_brief(workflow_id: uuid.UUID, container: ContainerDep) -> dict[str, Any]:
    try:
        return await container.workflows.brief(workflow_id)
    except WorkflowError as exc:
        raise _http(exc) from exc


@router.post("/workflows/{workflow_id}/report")
async def report_node(
    workflow_id: uuid.UUID, body: ReportRequest, container: ContainerDep
) -> dict[str, Any]:
    try:
        await container.workflows.report(
            workflow_id, node_id=body.node_id, reporter=body.reporter, outputs=body.outputs,
            evidence_ids=body.evidence_ids,
        )  # fmt: skip
        return await container.workflows.brief(workflow_id)
    except WorkflowError as exc:
        raise _http(exc) from exc


@router.post("/workflows/{workflow_id}/resume")
async def resume_workflow(workflow_id: uuid.UUID, container: ContainerDep) -> dict[str, Any]:
    """Re-check a gated node after a human decided the approval."""
    try:
        await container.workflows.resume(workflow_id)
        return await container.workflows.brief(workflow_id)
    except WorkflowError as exc:
        raise _http(exc) from exc


@router.post("/workflows/{workflow_id}/cancel")
async def cancel_workflow(
    workflow_id: uuid.UUID, body: CancelRequest, container: ContainerDep
) -> dict[str, Any]:
    try:
        await container.workflows.cancel(workflow_id, actor=body.actor, reason=body.reason)
        return await container.workflows.brief(workflow_id)
    except WorkflowError as exc:
        raise _http(exc) from exc
