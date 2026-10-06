"""HTTP adapters for the vendor-neutral :class:`LLMProvider` protocol (no vendor SDKs)."""

from __future__ import annotations

import ipaddress
import time
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx

from eios_llm.models import LLMError, LLMRequest, LLMResponse, LLMUsage

Locality = Literal["local", "remote"]
_LOCAL_NAMES = frozenset({"localhost", "host.docker.internal", "ollama", "llm", "model-server"})


class LLMProvider(Protocol):
    name: str
    locality: Locality
    default_model: str

    async def complete(self, request: LLMRequest) -> LLMResponse: ...


def _is_local(host: str) -> bool:
    if host in _LOCAL_NAMES:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def _raise_for(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    retryable = response.status_code in {408, 409, 429} or response.status_code >= 500
    detail = response.text[:300].replace("\n", " ")
    raise LLMError(
        f"provider returned HTTP {response.status_code}: {detail}",
        retryable=retryable,
        status=response.status_code,
    )


class _HttpProvider:
    name: str
    locality: Locality
    default_model: str

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        default_model: str,
        timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        parts = urlsplit(base_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError(f"invalid base URL for provider '{name}'")
        self.name = name
        self.default_model = default_model
        self.locality = "local" if _is_local(parts.hostname) else "remote"
        self._base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds, transport=transport, headers=headers or {}
        )

    @property
    def host(self) -> str:
        return urlsplit(self._base).hostname or ""

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        started = time.monotonic()
        try:
            response = await self._client.post(f"{self._base}{path}", json=payload)
        except httpx.TimeoutException as exc:
            raise LLMError("provider timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"provider unreachable: {type(exc).__name__}", retryable=True) from exc
        _raise_for(response)
        try:
            body = response.json()
        except ValueError as exc:
            raise LLMError("provider returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise LLMError("provider returned an unexpected payload")
        return body, int((time.monotonic() - started) * 1000)


class OpenAICompatibleProvider(_HttpProvider):
    """OpenAI Chat Completions wire format: OpenAI, vLLM, llama.cpp server, Ollama (/v1), LM Studio."""

    def __init__(
        self,
        *,
        name: str = "openai-compatible",
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        default_model: str,
        timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        super().__init__(
            name=name, base_url=base_url, default_model=default_model,
            timeout_seconds=timeout_seconds, transport=transport, headers=headers,
        )  # fmt: skip

    async def complete(self, request: LLMRequest) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": request.model or self.default_model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}
        body, latency = await self._post("/chat/completions", payload)
        try:
            choice = body["choices"][0]
            text = choice["message"]["content"] or ""
            finish = str(choice.get("finish_reason") or "stop")
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("provider response is missing choices") from exc
        usage = body.get("usage") or {}
        return LLMResponse(
            text=text, model=str(body.get("model", payload["model"])), provider=self.name,
            usage=LLMUsage(input_tokens=int(usage.get("prompt_tokens", 0)),
                           output_tokens=int(usage.get("completion_tokens", 0))),
            finish_reason=finish, latency_ms=latency,
        )  # fmt: skip


class LocalOpenAIProvider(OpenAICompatibleProvider):
    """A model server on this machine/network (local-first). Refuses public hosts by construction."""

    def __init__(self, *, base_url: str, default_model: str, **kwargs: Any) -> None:
        super().__init__(
            name=kwargs.pop("name", "local"), base_url=base_url, default_model=default_model,
            **kwargs,
        )  # fmt: skip
        if self.locality != "local":
            raise ValueError(
                f"'{self.host}' is not a local host; use OpenAICompatibleProvider for remote endpoints"
            )


class AnthropicProvider(_HttpProvider):
    """Anthropic Messages API over plain HTTP."""

    API_VERSION = "2023-06-01"

    def __init__(
        self,
        *,
        api_key: str,
        default_model: str,
        name: str = "anthropic",
        base_url: str = "https://api.anthropic.com",
        timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            name=name, base_url=base_url, default_model=default_model,
            timeout_seconds=timeout_seconds, transport=transport,
            headers={"x-api-key": api_key, "anthropic-version": self.API_VERSION},
        )  # fmt: skip

    async def complete(self, request: LLMRequest) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": request.model or self.default_model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": [{"role": m.role, "content": m.content} for m in request.turns],
        }
        if system := request.system:
            payload["system"] = system
        body, latency = await self._post("/v1/messages", payload)
        try:
            text = "".join(
                str(block.get("text", ""))
                for block in body["content"]
                if block.get("type") == "text"
            )
        except (KeyError, TypeError, AttributeError) as exc:
            raise LLMError("provider response is missing content") from exc
        usage = body.get("usage") or {}
        return LLMResponse(
            text=text, model=str(body.get("model", payload["model"])), provider=self.name,
            usage=LLMUsage(input_tokens=int(usage.get("input_tokens", 0)),
                           output_tokens=int(usage.get("output_tokens", 0))),
            finish_reason=str(body.get("stop_reason") or "stop"), latency_ms=latency,
        )  # fmt: skip


class ScriptedProvider:
    """Deterministic offline provider for tests and dry runs. Never touches the network."""

    locality: Locality = "local"

    def __init__(self, replies: list[str] | None = None, *, name: str = "scripted") -> None:
        self.name = name
        self.default_model = "scripted-1"
        self._replies = list(replies or ["ok"])
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        text = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        words = sum(len(m.content.split()) for m in request.messages)
        return LLMResponse(
            text=text, model=self.default_model, provider=self.name,
            usage=LLMUsage(input_tokens=words, output_tokens=len(text.split())),
        )  # fmt: skip
