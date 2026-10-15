"""Web research: which hosts agents may read pages from, and a way to try one."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from eios_api.deps import AdminTokenHeader, ContainerDep, check_admin_token
from eios_domain.events import ActorType
from eios_policy.runtime import ToolInvocation
from eios_runtime.web_research import WebResearchError

router = APIRouter(prefix="/web", tags=["web"])


class HostsBody(BaseModel):
    hosts: list[str] = Field(max_length=100)


class TryBody(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


def _admin(container: ContainerDep, token: str | None) -> None:
    check_admin_token(container, token, disabled="changing the web allowlist is disabled (no admin token is set)")


@router.get("")
async def web_status(container: ContainerDep) -> dict[str, Any]:
    await container.web.refresh()
    return container.web.describe()


@router.put("/hosts")
async def set_hosts(body: HostsBody, container: ContainerDep, token: AdminTokenHeader = None) -> dict[str, Any]:
    """Replace the allowlist (an operator decision: these hosts may be read by agents)."""
    _admin(container, token)
    try:
        await container.web.save(body.hosts)
    except WebResearchError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return container.web.describe()


@router.delete("/hosts")
async def reset_hosts(container: ContainerDep, token: AdminTokenHeader = None) -> dict[str, Any]:
    """Forget the list chosen here; the one from ``.env`` (if any) applies again."""
    _admin(container, token)
    await container.web.clear()
    return container.web.describe()


@router.post("/try")
async def try_page(body: TryBody, container: ContainerDep) -> dict[str, Any]:
    """Fetch one page through the same capability agents use (policy, allowlist, evidence)."""
    result = await container.tools.invoke(
        ToolInvocation(capability="web_research", arguments={"url": body.url, "max_chars": 1500},
                       actor_type=ActorType.HUMAN, actor_id="ui")
    )  # fmt: skip
    return result.summary()
