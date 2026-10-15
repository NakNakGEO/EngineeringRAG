"""Capability, tool, agent and skill registries and capability routing.

Listing endpoints are deliberately compact (what an LLM may see by default); detail endpoints
return the full manifest on demand.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from eios_api.deps import ContainerDep
from eios_capability import RoutingRequest

router = APIRouter(tags=["capabilities"])


class ResolveRequest(BaseModel):
    capability: str = Field(min_length=1, max_length=100)
    language: str | None = None
    extension: str | None = None
    kind: str | None = None
    path: str | None = None
    network_allowed: bool = False
    allow_experimental: bool = True
    run_id: uuid.UUID | None = Field(default=None, description="record routing events on this run")


@router.get("/capabilities")
async def list_capabilities(container: ContainerDep) -> list[dict[str, Any]]:
    return await container.registry.capability_summaries()


@router.get("/capabilities/{capability_id}")
async def capability_detail(capability_id: str, container: ContainerDep) -> dict[str, Any]:
    detail = await container.registry.capability_detail(capability_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"capability {capability_id} not found")
    return detail


@router.post("/capabilities/resolve")
async def resolve_capability(body: ResolveRequest, container: ContainerDep) -> dict[str, Any]:
    """Route a capability to a registered provider. Never guesses: unknown means unknown."""
    request = RoutingRequest(
        capability=body.capability,
        language=body.language,
        extension=body.extension,
        kind=body.kind,
        path=body.path,
        network_allowed=body.network_allowed,
        allow_experimental=body.allow_experimental,
    )
    ctx = await container.recorder.resume(body.run_id) if body.run_id else None
    return (await container.router.resolve(request, ctx)).summary()


class InvokeRequest(ResolveRequest):
    arguments: dict[str, Any] = Field(default_factory=dict)


@router.post("/capabilities/invoke")
async def invoke_capability(body: InvokeRequest, container: ContainerDep) -> dict[str, Any]:
    """Route, policy-check and execute a capability as an LLM-level actor (same path as MCP)."""
    from eios_domain.events import ActorType
    from eios_policy import ToolInvocation

    ctx = await container.recorder.resume(body.run_id) if body.run_id else None
    result = await container.tools.invoke(
        ToolInvocation(
            capability=body.capability.strip(),
            arguments=body.arguments,
            actor_type=ActorType.LLM,
            actor_id="http_client",
            routing=RoutingRequest(
                capability=body.capability.strip(), language=body.language,
                extension=body.extension, kind=body.kind, path=body.path,
                network_allowed=body.network_allowed, allow_experimental=body.allow_experimental,
            ),
        ),
        ctx,
    )  # fmt: skip
    return result.summary()


@router.get("/tools")
async def list_tools(container: ContainerDep) -> list[dict[str, Any]]:
    return await container.registry.provider_summaries()


@router.get("/tools/{tool_id}")
async def tool_detail(tool_id: str, container: ContainerDep) -> dict[str, Any]:
    provider = await container.registry.store.get_provider(tool_id)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"tool {tool_id} not found")
    return {
        **provider.model_dump(mode="json"),
        "history": [
            {k: str(v) for k, v in h.items()}
            for h in await container.registry.store.history("provider", tool_id)
        ],
    }


@router.get("/agents")
async def list_agents(container: ContainerDep) -> list[dict[str, Any]]:
    return await container.registry.agent_summaries()


@router.get("/agents/{agent_id}")
async def agent_detail(agent_id: str, container: ContainerDep) -> dict[str, Any]:
    agent = await container.registry.store.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"agent {agent_id} not found")
    return _jsonable(agent)


@router.get("/skills")
async def list_skills(container: ContainerDep) -> list[dict[str, Any]]:
    return await container.registry.skill_summaries()


@router.get("/skills/{skill_id}")
async def skill_detail(skill_id: str, container: ContainerDep) -> dict[str, Any]:
    skill = await container.registry.store.get_skill(skill_id)
    if skill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"skill {skill_id} not found")
    return _jsonable(skill)


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    return {
        k: (str(v) if isinstance(v, uuid.UUID) or hasattr(v, "isoformat") else v)
        for k, v in row.items()
    }
