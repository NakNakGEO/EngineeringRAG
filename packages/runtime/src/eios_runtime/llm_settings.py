"""The local language model, changeable at runtime.

``.env`` (read at start) gives the defaults. A local model chosen in the UI is stored in
``platform.setting`` and wins over ``.env``, in every process that reads the same database (the
API and the worker), without a restart. Only a *local* OpenAI-compatible server can be set this
way: a URL is checked to be on this machine / private network, and API keys for remote providers
stay in ``.env``; nothing secret is ever stored here.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from dataclasses import dataclass
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_core.settings import Settings
from eios_domain.ids import utcnow
from eios_llm import (
    AnthropicProvider,
    LLMGateway,
    LLMProvider,
    LocalOpenAIProvider,
    OpenAICompatibleProvider,
)
from eios_llm.providers import is_local_host, is_local_ip
from eios_storage.tables.platform import setting as setting_t

KEY = "llm.local"
_REFRESH_EVERY = 3.0  # seconds between looks at the database


@dataclass(frozen=True)
class LocalLlm:
    base_url: str
    model: str
    timeout_seconds: float = 240.0


class InvalidLocalLlm(ValueError):
    """The URL or model is not acceptable (not a local server, malformed...)."""


def validate(base_url: str, model: str, timeout_seconds: float) -> LocalLlm:
    from urllib.parse import urlsplit

    url = base_url.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise InvalidLocalLlm("the URL must look like http://host:port/v1")
    if not is_local_host(parts.hostname):
        raise InvalidLocalLlm(
            f"'{parts.hostname}' is not on this machine or a private network; "
            "remote providers are configured in .env, not here"
        )
    name = model.strip()
    if not name or len(name) > 200:
        raise InvalidLocalLlm("a model name is needed")
    if not 5 <= timeout_seconds <= 1800:
        raise InvalidLocalLlm("the timeout must be between 5 and 1800 seconds")
    return LocalLlm(url, name, float(timeout_seconds))


async def ensure_resolves_local(base_url: str) -> None:
    """Resolve the host and require every address it points to to be local/private (a name that
    merely *looks* local, or one re-pointed elsewhere, is refused)."""
    from urllib.parse import urlsplit

    parts = urlsplit(base_url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise InvalidLocalLlm(f"cannot resolve '{host}'") from exc
    addresses = {ipaddress.ip_address(i[4][0]) for i in infos}
    if not addresses or not all(is_local_ip(a) for a in addresses):
        raise InvalidLocalLlm(f"'{host}' does not point at this machine or a private network")


def providers_from(settings: Settings, local: LocalLlm | None) -> dict[str, LLMProvider]:
    """Every configured provider: the chosen local model (else the one from ``.env``), then the
    remote ones from ``.env``. A provider exists only when it is configured."""
    providers: dict[str, LLMProvider] = {}
    if local is not None:
        providers["local"] = LocalOpenAIProvider(
            base_url=local.base_url, default_model=local.model, timeout_seconds=local.timeout_seconds
        )
    elif settings.llm_local_base_url and settings.llm_local_model:
        providers["local"] = LocalOpenAIProvider(
            base_url=settings.llm_local_base_url,
            default_model=settings.llm_local_model,
            timeout_seconds=settings.llm_local_timeout_seconds,
        )
    if settings.llm_openai_api_key and settings.llm_openai_model:
        providers["openai"] = OpenAICompatibleProvider(
            name="openai",
            base_url=settings.llm_openai_base_url,
            api_key=settings.llm_openai_api_key.get_secret_value(),
            default_model=settings.llm_openai_model,
        )
    if settings.llm_anthropic_api_key and settings.llm_anthropic_model:
        providers["anthropic"] = AnthropicProvider(
            api_key=settings.llm_anthropic_api_key.get_secret_value(),
            default_model=settings.llm_anthropic_model,
        )
    return providers


class LlmSettings:
    def __init__(self, engine: AsyncEngine, env: Settings, gateway: LLMGateway) -> None:
        self._engine = engine
        self._env = env
        self._gateway = gateway
        self._applied: str | None = None  # the stored value's version we last applied
        self._checked = 0.0
        self._override: LocalLlm | None = None

    # -- reading ----------------------------------------------------------------------------
    async def _stored(self) -> tuple[dict[str, Any], str] | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    sa.select(setting_t.c.value, setting_t.c.updated_at).where(setting_t.c.key == KEY)
                )
            ).first()
        return (dict(row.value), row.updated_at.isoformat()) if row is not None else None

    async def refresh(self, *, force: bool = False) -> None:
        """Pick up a change made by another process. Cheap: one small query, at most every 3 s."""
        now = time.monotonic()
        if not force and now - self._checked < _REFRESH_EVERY:
            return
        self._checked = now
        try:
            stored = await self._stored()
        except Exception:  # the database being away must not take the model features down with it
            return
        version = stored[1] if stored else None
        if version == self._applied and not force:
            return
        override: LocalLlm | None = None
        if stored is not None:
            try:
                v = stored[0]
                override = validate(str(v["base_url"]), str(v["model"]), float(v.get("timeout_seconds", 240)))
            except (KeyError, ValueError):
                override = None
        self._override = override
        self._gateway.set_providers(providers_from(self._env, override), self._env.llm_default_provider)
        self._applied = version

    # -- changing ---------------------------------------------------------------------------
    async def save(self, cfg: LocalLlm) -> None:
        value = {"base_url": cfg.base_url, "model": cfg.model, "timeout_seconds": cfg.timeout_seconds}
        async with self._engine.begin() as conn:
            await conn.execute(
                pg_insert(setting_t)
                .values(key=KEY, value=value, updated_at=utcnow())
                .on_conflict_do_update(index_elements=["key"], set_={"value": value, "updated_at": utcnow()})
            )
        await self.refresh(force=True)

    async def clear(self) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(sa.delete(setting_t).where(setting_t.c.key == KEY))
        await self.refresh(force=True)

    # -- describing -------------------------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        """Where the local model comes from and what it is (never any secret)."""
        if self._override is not None:
            o = self._override
            return {"source": "ui", "base_url": o.base_url, "model": o.model, "timeout_seconds": o.timeout_seconds}
        if self._env.llm_local_base_url and self._env.llm_local_model:
            return {
                "source": "env", "base_url": self._env.llm_local_base_url, "model": self._env.llm_local_model,
                "timeout_seconds": self._env.llm_local_timeout_seconds,
            }  # fmt: skip
        return {"source": "none", "base_url": None, "model": None, "timeout_seconds": self._env.llm_local_timeout_seconds}


async def list_models(base_url: str, *, timeout: float = 8.0) -> list[str]:
    """Model names an OpenAI-compatible local server offers (``GET <base_url>/models``)."""
    cfg = validate(base_url, "probe", 30)
    await ensure_resolves_local(cfg.base_url)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        r = await client.get(f"{cfg.base_url}/models")
    r.raise_for_status()
    body = r.json()
    items = body.get("data") if isinstance(body, dict) else None
    return sorted(str(m.get("id")) for m in (items or []) if isinstance(m, dict) and m.get("id"))
