"""Provider health checks: does the thing the manifest claims actually exist here?"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from eios_capability.adapters import AdapterCatalog
from eios_capability.manifests import ToolManifest
from eios_capability.store import ProviderRow, RegistryStore


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    detail: str


def _check_subprocess(manifest: ToolManifest) -> list[str]:
    path = Path(manifest.entrypoint.split(":", 1)[1])
    if not path.is_absolute() or not path.is_file():
        return ["executable is missing or not an absolute path"]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return [] if digest == manifest.sha256 else ["executable does not match its pinned SHA-256"]


def check_provider(provider: ProviderRow, adapters: AdapterCatalog) -> HealthResult:
    """Pure, side-effect-free check (never executes the provider)."""
    manifest = ToolManifest.model_validate(provider.manifest)
    problems: list[str] = []
    scheme = manifest.entrypoint.split(":", 1)[0]
    if scheme in {"builtin", "adapter"}:
        if adapters.get(manifest.entrypoint) is None:
            problems.append(f"no implementation for '{manifest.entrypoint}'")
    elif scheme == "mcp":
        problems.append("mcp providers are not connected in this deployment")
    elif scheme == "subprocess":
        problems.extend(_check_subprocess(manifest))
    for req in manifest.environment_requirements:
        if req.kind == "binary" and shutil.which(req.name) is None:
            problems.append(f"missing binary '{req.name}'")
        elif req.kind == "env" and not os.environ.get(req.name):
            problems.append(f"missing environment variable '{req.name}'")
    return HealthResult(not problems, "; ".join(problems) or "ok")


class HealthChecker:
    def __init__(self, store: RegistryStore, adapters: AdapterCatalog) -> None:
        self._store = store
        self._adapters = adapters

    async def run_all(self) -> dict[str, HealthResult]:
        results: dict[str, HealthResult] = {}
        for p in await self._store.list_providers():
            result = check_provider(p, self._adapters)
            await self._store.set_provider_health(
                p.id, p.version, "ok" if result.ok else "fail", result.detail
            )
            results[f"{p.id}@{p.version}"] = result
        return results
