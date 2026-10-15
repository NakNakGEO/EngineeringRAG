"""Adapter catalog: real in-process implementations behind ``builtin:``/``adapter:`` entrypoints.

A provider manifest is only a *claim*. The catalog holds the code that actually exists; a
manifest whose entrypoint is not in the catalog is reported unhealthy and is never routable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

AdapterFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Adapter:
    name: str
    fn: AdapterFn
    description: str = ""


class AdapterCatalog:
    def __init__(self) -> None:
        self._adapters: dict[str, Adapter] = {}

    def register(self, name: str, fn: AdapterFn, description: str = "") -> None:
        if name in self._adapters:
            raise ValueError(f"adapter '{name}' is already registered")
        self._adapters[name] = Adapter(name, fn, description)

    def get(self, entrypoint: str) -> Adapter | None:
        """Resolve ``builtin:x`` / ``adapter:x``; other schemes are never resolved here."""
        scheme, _, name = entrypoint.partition(":")
        if scheme not in {"builtin", "adapter"}:
            return None
        return self._adapters.get(name)

    def names(self) -> list[str]:
        return sorted(self._adapters)

    async def invoke(self, entrypoint: str, arguments: dict[str, Any]) -> dict[str, Any]:
        adapter = self.get(entrypoint)
        if adapter is None:
            raise LookupError(f"no adapter implements '{entrypoint}'")
        return await adapter.fn(arguments)
