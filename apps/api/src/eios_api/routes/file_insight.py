"""Per-file insight for the details panel: what changed, and a few words on what the file is for."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from eios_api.deps import ContainerDep
from eios_domain.errors import NotFoundError
from eios_project_intelligence import SummaryState

router = APIRouter(prefix="/projects", tags=["file-insight"])

Path = Annotated[str, Query(min_length=1, max_length=2000)]


class SummaryRequest(BaseModel):
    path: str = Field(min_length=1, max_length=2000)
    force: bool = Field(default=False, description="write a new summary even if this version has one")


async def _require(container: ContainerDep, project_id: uuid.UUID) -> None:
    await container.llm_settings.refresh()  # the model may have been changed in the UI
    try:
        await container.projects.require(project_id)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found") from exc


@router.get("/{project_id}/file/summary", response_model=SummaryState)
async def get_summary(project_id: uuid.UUID, container: ContainerDep, path: Path) -> SummaryState:
    """The stored summary and whether it still matches the file. Never calls a model."""
    await _require(container, project_id)
    try:
        return await container.insight.state(project_id, path)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/{project_id}/file/summary", response_model=SummaryState)
async def make_summary(project_id: uuid.UUID, body: SummaryRequest, container: ContainerDep) -> SummaryState:
    """Summarise the file as it is now (one local-model call at most; reuses the stored one)."""
    await _require(container, project_id)
    try:
        return await container.insight.summarise(project_id, body.path, force=body.force)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/{project_id}/file/diff")
async def file_diff(
    project_id: uuid.UUID,
    container: ContainerDep,
    path: Path,
    counts_only: Annotated[bool, Query(description="skip the changed lines, return only the counts")] = False,
) -> dict[str, Any]:
    """Uncommitted changes of one file: the working copy against the last commit."""
    await _require(container, project_id)
    try:
        out = await container.insight.diff(project_id, path)
        if counts_only:
            out["hunks"] = []
        return out
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
