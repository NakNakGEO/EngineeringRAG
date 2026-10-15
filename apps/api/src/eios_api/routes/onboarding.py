"""Tool onboarding, step one: download a repository to inspect it (it is never run)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from eios_api.deps import AdminTokenHeader, ContainerDep, check_admin_token
from eios_runtime.onboarding import OnboardingError

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


class InspectBody(BaseModel):
    url: str = Field(min_length=1, max_length=300)
    ref: str = Field(default="HEAD", min_length=1, max_length=100)


class DecideBody(BaseModel):
    state: str = Field(pattern="^(reviewed|rejected)$")
    by: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=1000)


@router.post("/inspect", status_code=status.HTTP_201_CREATED)
async def inspect(body: InspectBody, container: ContainerDep, token: AdminTokenHeader = None) -> dict[str, Any]:
    """Download the repository archive (allowlisted hosts only), scan it in memory, keep the report."""
    check_admin_token(container, token, disabled="onboarding is disabled (no admin token is set)")
    try:
        return await container.onboarding.inspect(body.url, body.ref, requested_by="ui")
    except OnboardingError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.get("")
async def candidates(container: ContainerDep) -> list[dict[str, Any]]:
    return await container.onboarding.list()


@router.post("/{candidate_id}/decision")
async def decide(candidate_id: uuid.UUID, body: DecideBody, container: ContainerDep, token: AdminTokenHeader = None) -> dict[str, Any]:
    """A human marks the inspected repository reviewed (fit for a sandbox trial) or rejected."""
    check_admin_token(container, token, disabled="onboarding is disabled (no admin token is set)")
    try:
        return await container.onboarding.decide(candidate_id, body.state, body.by, body.note)
    except OnboardingError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
