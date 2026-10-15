"""Which language models the platform may use, how to change the local one, and a one-line test.

Reading is open (like the rest of the read-only UI). Changing the local model needs the admin
token: it decides where project text is sent. Only a server on this machine / private network
can be set here; remote providers and their keys stay in ``.env``.
"""

from __future__ import annotations

import time
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from eios_api.deps import AdminTokenHeader, ContainerDep, check_admin_token
from eios_llm import LLMError, LLMMessage, LLMRequest
from eios_runtime.llm_settings import (
    InvalidLocalLlmError,
    ensure_resolves_local,
    list_models,
    validate,
)

router = APIRouter(prefix="/llm", tags=["llm"])


class ProviderInfo(BaseModel):
    name: str
    locality: str
    default_model: str


class LocalSource(BaseModel):
    source: str  # "ui" (chosen here) | "env" (.env) | "none"
    base_url: str | None
    model: str | None
    timeout_seconds: float


class LlmStatus(BaseModel):
    configured: bool
    default: str | None
    providers: list[ProviderInfo]
    local: LocalSource
    how_to_configure: str


class LocalBody(BaseModel):
    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    timeout_seconds: float = Field(default=240.0, ge=5, le=1800)


class LlmTestResult(BaseModel):
    ok: bool
    provider: str | None = None
    model: str | None = None
    latency_ms: int | None = None
    reply: str | None = None
    error: str | None = None


HOW_TO = (
    "Choose a server on this PC below (for Ollama in Docker use "
    "http://host.docker.internal:11434/v1), or set EIOS_LLM_LOCAL_BASE_URL and "
    "EIOS_LLM_LOCAL_MODEL in .env and restart."
)


def _require_admin(container: ContainerDep, token: str | None) -> None:
    check_admin_token(
        container, token, disabled="changing the model is disabled (no admin token is set)"
    )


async def _status(container: ContainerDep) -> LlmStatus:
    await container.llm_settings.refresh()
    providers = [ProviderInfo(**p) for p in container.llm.describe()]
    default = container.settings.llm_default_provider
    if default not in {p.name for p in providers}:
        default = providers[0].name if providers else None
    return LlmStatus(
        configured=bool(providers), default=default, providers=providers,
        local=LocalSource(**container.llm_settings.describe()), how_to_configure=HOW_TO,
    )  # fmt: skip


@router.get("", response_model=LlmStatus)
async def llm_status(container: ContainerDep) -> LlmStatus:
    return await _status(container)


@router.get("/models", response_model=list[str])
async def server_models(
    base_url: Annotated[str, Query(min_length=1, max_length=500)],
    container: ContainerDep,
    token: AdminTokenHeader = None,
) -> list[str]:
    """The models a local OpenAI-compatible server (Ollama, LM Studio...) offers. The server makes
    this request, so it is admin-only and limited to this machine / private networks."""
    _require_admin(container, token)
    try:
        return await list_models(base_url)
    except InvalidLocalLlmError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"could not read the model list: {type(exc).__name__}"
        ) from exc


@router.put("/local", response_model=LlmStatus)
async def set_local(
    body: LocalBody, container: ContainerDep, token: AdminTokenHeader = None
) -> LlmStatus:
    """Use this local model from now on (stored; the API and the worker pick it up at once)."""
    _require_admin(container, token)
    try:
        cfg = validate(body.base_url, body.model, body.timeout_seconds)
        await ensure_resolves_local(cfg.base_url)
    except InvalidLocalLlmError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    await container.llm_settings.save(cfg)
    return await _status(container)


@router.delete("/local", response_model=LlmStatus)
async def reset_local(container: ContainerDep, token: AdminTokenHeader = None) -> LlmStatus:
    """Forget the model chosen here; the one from ``.env`` (if any) applies again."""
    _require_admin(container, token)
    await container.llm_settings.clear()
    return await _status(container)


@router.post("/test", response_model=LlmTestResult)
async def llm_test(container: ContainerDep) -> LlmTestResult:
    """One tiny request to the default model, so a wrong URL or model name shows up at once."""
    await container.llm_settings.refresh()
    if not container.llm.names:
        return LlmTestResult(ok=False, error="no model is configured")
    started = time.monotonic()
    try:
        reply = await container.llm.complete(
            LLMRequest(
                messages=[LLMMessage(role="user", content="Reply with the single word: ready")],
                max_tokens=64, temperature=0.0, data_classification="public", reasoning="none",
            ),
            actor_id="llm_test",
        )  # fmt: skip
    except LLMError as exc:
        return LlmTestResult(ok=False, error=str(exc)[:300])
    result: dict[str, Any] = {
        "ok": True, "provider": reply.provider, "model": reply.model,
        "latency_ms": int((time.monotonic() - started) * 1000), "reply": reply.text.strip()[:80],
    }  # fmt: skip
    return LlmTestResult(**result)
