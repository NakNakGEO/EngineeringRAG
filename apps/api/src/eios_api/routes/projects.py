"""Project intelligence endpoints: bootstrap, sync, inventory, symbols, graph, coverage."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from eios_api.deps import ContainerDep
from eios_domain.errors import NotFoundError
from eios_domain.project import BootstrapState, FileScope
from eios_project_intelligence import Project, SyncResult, WorkspaceViolationError
from eios_project_intelligence.store import FileRow, SymbolRow

router = APIRouter(prefix="/projects", tags=["projects"])


class BootstrapRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    sync: Literal["never", "if_needed", "always"] = "if_needed"
    wait: bool = Field(default=False, description="run the sync inline instead of queueing it")


class SyncSummary(BaseModel):
    branch: str
    commit: str
    state: BootstrapState
    files_total: int
    changed: int
    removed: int
    overlay: int
    symbols_total: int
    duration_ms: int

    @classmethod
    def of(cls, r: SyncResult) -> SyncSummary:
        return cls(
            branch=r.branch,
            commit=r.commit,
            state=r.state,
            files_total=r.files_total,
            changed=len(r.changed),
            removed=len(r.removed),
            overlay=len(r.overlay),
            symbols_total=r.symbols_total,
            duration_ms=r.duration_ms,
        )


class BootstrapResponse(BaseModel):
    state: BootstrapState
    project: Project | None
    branch: str | None = None
    head: str | None = None
    dirty_paths: int = 0
    needs_sync: bool = False
    previous_branch: str | None = None
    previous_commit: str | None = None
    detail: str | None = None
    sync: SyncSummary | None = None
    job_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None


class SyncRequest(BaseModel):
    wait: bool = False


class SyncResponse(BaseModel):
    run_id: uuid.UUID
    job_id: uuid.UUID | None = None
    sync: SyncSummary | None = None


def _forbidden(exc: WorkspaceViolationError) -> HTTPException:
    return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))


@router.post("/bootstrap", response_model=BootstrapResponse)
async def bootstrap_project(body: BootstrapRequest, container: ContainerDep) -> BootstrapResponse:
    """Identify the project at ``path`` (only inside approved workspaces) and report whether its
    index is current. Optionally queue or run a sync."""
    try:
        result = await container.projects.bootstrap(body.path)
    except WorkspaceViolationError as exc:
        raise _forbidden(exc) from exc
    response = BootstrapResponse(
        state=result.state,
        project=result.project,
        branch=result.branch,
        head=result.head,
        dirty_paths=result.dirty_paths,
        needs_sync=result.needs_sync,
        previous_branch=result.previous_branch,
        previous_commit=result.previous_commit,
        detail=result.detail,
    )
    wanted = body.sync == "always" or (body.sync == "if_needed" and result.needs_sync)
    if result.project is not None and result.state is not BootstrapState.ERROR and wanted:
        if body.wait:
            sync, run_id = await container.projects.sync_now(result.project.id)
            response.sync, response.run_id = SyncSummary.of(sync), run_id
        else:
            job, run_id = await container.projects.request_sync(result.project.id)
            response.job_id, response.run_id = job.id, run_id
    return response


@router.get("", response_model=list[Project])
async def list_projects(container: ContainerDep) -> list[Project]:
    return await container.projects.store.list_projects()


async def _project(container: ContainerDep, project_id: uuid.UUID) -> Project:
    try:
        return await container.projects.require(project_id)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found") from exc


class ProjectDetail(BaseModel):
    project: Project
    latest_snapshot: dict[str, Any] | None


@router.get("/{project_id}", response_model=ProjectDetail)
async def get_project(project_id: uuid.UUID, container: ContainerDep) -> ProjectDetail:
    project = await _project(container, project_id)
    return ProjectDetail(
        project=project, latest_snapshot=await container.projects.store.latest_snapshot(project_id)
    )


@router.post(
    "/{project_id}/sync", response_model=SyncResponse, status_code=status.HTTP_202_ACCEPTED
)
async def sync_project(
    project_id: uuid.UUID, body: SyncRequest, container: ContainerDep
) -> SyncResponse:
    await _project(container, project_id)
    try:
        if body.wait:
            sync, run_id = await container.projects.sync_now(project_id)
            return SyncResponse(run_id=run_id, sync=SyncSummary.of(sync))
        job, run_id = await container.projects.request_sync(project_id)
    except WorkspaceViolationError as exc:
        raise _forbidden(exc) from exc
    return SyncResponse(run_id=run_id, job_id=job.id)


@router.get("/{project_id}/files", response_model=list[FileRow])
async def list_files(
    project_id: uuid.UUID,
    container: ContainerDep,
    branch: str | None = None,
    scope: FileScope = FileScope.COMMITTED,
    after_path: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    language: str | None = None,
    prefix: str | None = None,
) -> list[FileRow]:
    await _project(container, project_id)
    try:
        return await container.projects.list_files(
            project_id,
            branch=branch,
            scope=scope,
            after_path=after_path,
            limit=limit,
            language=language,
            path_prefix=prefix,
        )
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/{project_id}/symbols", response_model=list[SymbolRow])
async def search_symbols(
    project_id: uuid.UUID,
    container: ContainerDep,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    branch: str | None = None,
    include_overlay: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[SymbolRow]:
    await _project(container, project_id)
    try:
        return await container.projects.search_symbols(
            project_id, q, branch=branch, include_overlay=include_overlay, limit=limit
        )
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/{project_id}/graph")
async def graph_neighborhood(
    project_id: uuid.UUID,
    container: ContainerDep,
    key: Annotated[str, Query(min_length=1, max_length=1000)],
    branch: str | None = None,
    include_overlay: bool = False,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> dict[str, Any]:
    """One-hop neighbourhood of a node key, for incremental graph loading in the UI."""
    await _project(container, project_id)
    try:
        return await container.projects.neighborhood(
            project_id, key, branch=branch, include_overlay=include_overlay, limit=limit
        )
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/{project_id}/coverage")
async def coverage(
    project_id: uuid.UUID, container: ContainerDep, branch: str | None = None
) -> dict[str, Any]:
    await _project(container, project_id)
    try:
        return await container.projects.semantic_coverage(project_id, branch=branch)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
