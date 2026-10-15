"""Memory: short facts the platform should remember about a project (or, with the admin token,
about the owner). Written by clients through the API or the ``memory_write`` capability; always
recorded as client-written. The portable (Default Vault) export never includes project memory."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from eios_api.deps import AdminTokenHeader, ContainerDep, check_admin_token
from eios_domain.errors import DomainError
from eios_domain.knowledge import MemoryCreate
from eios_domain.vault import Vault

router = APIRouter(prefix="/memory", tags=["memory"])


class MemoryBody(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    project_id: uuid.UUID | None = None
    scope: str = Field(default="project", pattern="^(user|project|session)$")
    tags: list[str] = Field(default_factory=list, max_length=50)
    importance: float = Field(default=0.5, ge=0, le=1)
    created_by: str = Field(default="api", min_length=1, max_length=200)


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_memory(
    body: MemoryBody, container: ContainerDep, token: AdminTokenHeader = None
) -> dict[str, Any]:
    """Remember something. Without a project it goes to the Default Vault (portable), which is a
    human decision and needs the admin token."""
    if body.project_id is None:
        check_admin_token(
            container,
            token,
            disabled="memory without a project needs the admin token, which is not set",
        )
    try:
        item = await container.knowledge.add_memory(
            MemoryCreate(
                vault=Vault.PROJECT if body.project_id else Vault.DEFAULT,
                project_id=body.project_id,
                scope=body.scope,
                content=body.content,
                tags=body.tags,
                importance=body.importance,
                created_by=body.created_by,
            )
        )
    except (DomainError, ValueError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return item.model_dump(mode="json")


@router.get("")
async def list_memory(
    container: ContainerDep,
    project_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict[str, Any]]:
    items = await container.knowledge.memory.list_items(project_id=project_id, limit=limit)
    return [i.model_dump(mode="json") for i in items]


@router.delete("/{memory_id}")
async def forget(
    memory_id: uuid.UUID, container: ContainerDep, token: AdminTokenHeader = None
) -> dict[str, str]:
    """Forget one memory (a human act)."""
    check_admin_token(container, token, disabled="forgetting is disabled (no admin token is set)")
    if not await container.knowledge.memory.delete(memory_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "memory not found")
    return {"status": "forgotten"}
