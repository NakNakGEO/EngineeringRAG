"""LLM gateway: named providers, policy check, budgets, bounded retries, observable calls.

Engineering OS itself rarely calls a model (the primary reasoning model is the external client),
but internal features - the Capability Workshop's generators, curation helpers, evaluations - do,
and they go through here so that every call is governed the same way.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from eios_domain.events import ActorType, EventStatus, EventType
from eios_llm.models import LLMError, LLMRequest, LLMResponse
from eios_llm.providers import LLMProvider
from eios_observability.recorder import RunContext

MAX_ATTEMPTS = 3
# (provider name, locality, data classification) -> may the call proceed?
CallGuard = Callable[[str, str, str], Awaitable[tuple[bool, str]]]


class LLMGateway:
    def __init__(
        self,
        providers: dict[str, LLMProvider] | None = None,
        *,
        default: str | None = None,
        guard: CallGuard | None = None,
        backoff_seconds: float = 0.5,
    ) -> None:
        self._providers = dict(providers or {})
        self._default = default or next(iter(self._providers), None)
        self._guard = guard
        self._backoff = backoff_seconds

    def register(self, provider: LLMProvider) -> None:
        if provider.name in self._providers:
            raise ValueError(f"provider '{provider.name}' is already registered")
        self._providers[provider.name] = provider
        self._default = self._default or provider.name

    @property
    def names(self) -> list[str]:
        return sorted(self._providers)

    def describe(self) -> list[dict[str, str]]:
        return [
            {"name": p.name, "locality": p.locality, "default_model": p.default_model}
            for p in self._providers.values()
        ]

    async def complete(
        self,
        request: LLMRequest,
        *,
        provider: str | None = None,
        ctx: RunContext | None = None,
        actor_id: str = "eios",
    ) -> LLMResponse:
        name = provider or self._default
        if name is None or name not in self._providers:
            raise LLMError(f"no LLM provider '{name}' is configured")
        llm = self._providers[name]
        if self._guard is not None:
            allowed, reason = await self._guard(name, llm.locality, request.data_classification)
            if not allowed:
                raise LLMError(f"policy denied the call to '{name}': {reason}")
        last: LLMError | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if ctx is not None:
                await ctx.charge("llm_calls")
                await ctx.emit(
                    EventType.LLM_STARTED, f"{name} call started", status=EventStatus.STARTED,
                    data={"provider": name, "locality": llm.locality, "attempt": attempt,
                          "model": request.model or llm.default_model,
                          "classification": request.data_classification},
                    actor_type=ActorType.LLM, actor_id=actor_id,
                )  # fmt: skip
            try:
                response = await llm.complete(request)
            except LLMError as exc:
                last = exc
                if ctx is not None:
                    await ctx.emit(
                        EventType.LLM_COMPLETED, f"{name} call failed", status=EventStatus.FAILED,
                        data={"provider": name, "error": str(exc)[:300], "retry": exc.retryable},
                        actor_type=ActorType.LLM, actor_id=actor_id,
                    )  # fmt: skip
                if not exc.retryable or attempt == MAX_ATTEMPTS:
                    raise
                if ctx is not None:
                    await ctx.charge("retries")
                await asyncio.sleep(self._backoff * 2 ** (attempt - 1))
                continue
            if ctx is not None:
                await ctx.charge("tokens", response.usage.total)
                await ctx.emit(
                    EventType.LLM_COMPLETED, f"{name} call completed",
                    data={"provider": name, "model": response.model,
                          "input_tokens": response.usage.input_tokens,
                          "output_tokens": response.usage.output_tokens,
                          "latency_ms": response.latency_ms, "finish": response.finish_reason},
                    actor_type=ActorType.LLM, actor_id=actor_id,
                )  # fmt: skip
            return response
        raise last or LLMError("LLM call failed")
