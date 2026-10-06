"""Manifest models and the safe loader.

Manifests are *data*: YAML parsed with ``safe_load`` into strictly validated Pydantic models
(unknown fields rejected, sizes bounded). Loading a manifest never executes anything. A manifest
that names a forbidden capability (external database access) is invalid by construction.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from eios_domain.policy import Risk
from eios_domain.registry import (
    ID_PATTERN,
    VERSION_PATTERN,
    RegistryState,
    is_forbidden_capability,
)

MAX_MANIFEST_BYTES = 1_000_000
MAX_PROMPT_CHARS = 20_000
ENTRYPOINT = re.compile(r"^(builtin|adapter|mcp|subprocess):[A-Za-z0-9_./:-]{1,200}$")
TARGET_PATTERN = re.compile(r"^(\*|(language|ext|kind|path):[^\s]{1,120})$")


def _check_id(value: str) -> str:
    if not ID_PATTERN.match(value):
        raise ValueError(f"invalid id {value!r} (lowercase letters, digits, '_', '.', '-')")
    return value


def _check_capabilities(values: list[str]) -> list[str]:
    for v in values:
        _check_id(v)
        if is_forbidden_capability(v):
            raise ValueError(
                f"capability '{v}' is forbidden by Root Policy and can never be registered"
            )
    return values


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CapabilityManifest(_Strict):
    kind: Literal["capability"] = "capability"
    id: str
    description: str = Field(min_length=10, max_length=600)
    risk: Risk
    inputs: list[str] = Field(default_factory=list, max_length=30)
    outputs: list[str] = Field(default_factory=list, max_length=30)
    allowed_scopes: list[str] = Field(default_factory=list, max_length=30)
    tags: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        _check_id(v)
        if is_forbidden_capability(v):
            raise ValueError(f"capability '{v}' is forbidden by Root Policy")
        return v


class FilesystemScope(_Strict):
    scope: str = Field(min_length=1, max_length=100)  # e.g. approved_workspace, sandbox_output
    mode: Literal["read", "write"] = "read"


class Permissions(_Strict):
    filesystem: list[FilesystemScope] = Field(default_factory=list, max_length=20)
    network: Literal["none", "allowlist"] = "none"
    network_hosts: list[str] = Field(default_factory=list, max_length=50)
    process: list[str] = Field(default_factory=list, max_length=20)
    secrets: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _hosts_need_allowlist(self) -> Permissions:
        if self.network_hosts and self.network != "allowlist":
            raise ValueError("network_hosts requires network: allowlist")
        return self


class EnvironmentRequirement(_Strict):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["binary", "env", "service"] = "binary"


class HealthSpec(_Strict):
    check: str = Field(default="adapter_available", min_length=1, max_length=100)
    interval_seconds: int = Field(default=300, ge=10, le=86_400)


class ToolManifest(_Strict):
    kind: Literal["tool"] = "tool"
    id: str
    version: str
    type: Literal["builtin", "adapter", "mcp", "subprocess", "subsystem"] = "builtin"
    description: str = Field(min_length=10, max_length=600)
    capabilities: list[str] = Field(min_length=1, max_length=20)
    supported_targets: list[str] = Field(default_factory=lambda: ["*"], max_length=30)
    unsupported_targets: list[str] = Field(default_factory=list, max_length=30)
    input_contract: dict[str, Any] = Field(default_factory=dict)
    output_contract: dict[str, Any] = Field(default_factory=dict)
    permissions: Permissions = Field(default_factory=Permissions)
    network_required: bool = False
    filesystem_scopes: list[str] = Field(default_factory=list, max_length=20)
    environment_requirements: list[EnvironmentRequirement] = Field(
        default_factory=list, max_length=20
    )
    health_check: HealthSpec = Field(default_factory=HealthSpec)
    maturity: Literal["experimental", "beta", "stable"] = "experimental"
    trust: Literal["unrated", "community", "reviewed", "owner"] = "unrated"
    verification_state: Literal["unverified", "tests_passed", "evaluated"] = "unverified"
    priority: int = Field(default=50, ge=0, le=100)
    use_when: list[str] = Field(default_factory=list, max_length=20)
    avoid_when: list[str] = Field(default_factory=list, max_length=20)
    version_compatibility: dict[str, str] = Field(default_factory=dict)
    entrypoint: str
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    state: RegistryState = RegistryState.EXPERIMENTAL

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _check_id(v)

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        if not VERSION_PATTERN.match(v):
            raise ValueError("version must look like 1.2.3")
        return v

    @field_validator("capabilities")
    @classmethod
    def _caps(cls, v: list[str]) -> list[str]:
        return _check_capabilities(v)

    @field_validator("supported_targets", "unsupported_targets")
    @classmethod
    def _targets(cls, v: list[str]) -> list[str]:
        for t in v:
            if not TARGET_PATTERN.match(t):
                raise ValueError(
                    f"bad target pattern {t!r} (use *, language:x, ext:.x, kind:x, path:x)"
                )
        return v

    @model_validator(mode="after")
    def _consistency(self) -> ToolManifest:
        if not ENTRYPOINT.match(self.entrypoint):
            raise ValueError(
                "entrypoint must be builtin:<name>, adapter:<name>, mcp:<name> or subprocess:<path>"
            )
        if self.entrypoint.startswith("subprocess:") and self.sha256 is None:
            raise ValueError("subprocess entrypoints must be hash-pinned (sha256)")
        if ".." in self.entrypoint.split(":", 1)[1].split("/"):
            raise ValueError("entrypoint must not contain '..'")
        if self.network_required and self.permissions.network == "none":
            raise ValueError("network_required needs permissions.network: allowlist")
        if self.permissions.network == "allowlist" and not self.network_required:
            raise ValueError("permissions.network allowlist set but network_required is false")
        return self


class AgentPermissions(_Strict):
    write: bool = False
    network: bool = False
    fs_scopes: list[str] = Field(default_factory=list, max_length=20)


class AgentManifest(_Strict):
    kind: Literal["agent"] = "agent"
    id: str
    version: str
    role: str = Field(min_length=3, max_length=100)
    description: str = Field(min_length=10, max_length=600)
    prompt: str = Field(min_length=20, max_length=MAX_PROMPT_CHARS)
    capabilities: list[str] = Field(default_factory=list, max_length=50)
    allowed_tools: list[str] = Field(default_factory=list, max_length=50)
    permissions: AgentPermissions = Field(default_factory=AgentPermissions)
    forbidden_actions: list[str] = Field(default_factory=list, max_length=50)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    review_requirements: list[str] = Field(default_factory=list, max_length=20)
    can_write: bool = False
    state: RegistryState = RegistryState.EXPERIMENTAL

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _check_id(v)

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        if not VERSION_PATTERN.match(v):
            raise ValueError("version must look like 1.2.3")
        return v

    @field_validator("capabilities")
    @classmethod
    def _caps(cls, v: list[str]) -> list[str]:
        return _check_capabilities(v)

    @model_validator(mode="after")
    def _writer_consistency(self) -> AgentManifest:
        if self.can_write != self.permissions.write:
            raise ValueError("can_write must equal permissions.write")
        return self


class SkillStep(_Strict):
    id: str = Field(min_length=1, max_length=60)
    instruction: str = Field(min_length=3, max_length=2000)
    capability: str | None = None
    decision: str | None = Field(default=None, max_length=1000)


class SkillManifest(_Strict):
    kind: Literal["skill"] = "skill"
    id: str
    version: str
    goal: str = Field(min_length=10, max_length=600)
    trigger: str = Field(min_length=3, max_length=600)
    prerequisites: list[str] = Field(default_factory=list, max_length=20)
    required_capabilities: list[str] = Field(default_factory=list, max_length=30)
    procedure: list[SkillStep] = Field(min_length=1, max_length=40)
    completion_criteria: list[str] = Field(min_length=1, max_length=20)
    failure_conditions: list[str] = Field(default_factory=list, max_length=20)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    verification: list[str] = Field(default_factory=list, max_length=20)
    observability_events: list[str] = Field(default_factory=list, max_length=20)
    state: RegistryState = RegistryState.EXPERIMENTAL

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _check_id(v)

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        if not VERSION_PATTERN.match(v):
            raise ValueError("version must look like 1.2.3")
        return v

    @field_validator("required_capabilities")
    @classmethod
    def _caps(cls, v: list[str]) -> list[str]:
        return _check_capabilities(v)

    @model_validator(mode="after")
    def _steps(self) -> SkillManifest:
        ids = [s.id for s in self.procedure]
        if len(ids) != len(set(ids)):
            raise ValueError("procedure step ids must be unique")
        for step in self.procedure:
            if step.capability is not None:
                _check_capabilities([step.capability])
                if step.capability not in self.required_capabilities:
                    raise ValueError(
                        f"step '{step.id}' uses capability '{step.capability}' "
                        "that is not listed in required_capabilities"
                    )
        return self


Manifest = Annotated[
    CapabilityManifest | ToolManifest | AgentManifest | SkillManifest, Field(discriminator="kind")
]


class ManifestError(Exception):
    """A manifest document that could not be accepted."""

    def __init__(self, source: str, message: str) -> None:
        super().__init__(f"{source}: {message}")
        self.source = source
        self.message = message


def manifest_hash(manifest: BaseModel) -> str:
    canonical = json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


_KINDS: dict[str, type[BaseModel]] = {
    "capability": CapabilityManifest,
    "tool": ToolManifest,
    "agent": AgentManifest,
    "skill": SkillManifest,
}


def parse_document(
    raw: object, source: str
) -> CapabilityManifest | ToolManifest | AgentManifest | SkillManifest:
    if not isinstance(raw, dict):
        raise ManifestError(source, "a manifest document must be a mapping")
    kind = raw.get("kind")
    model = _KINDS.get(str(kind))
    if model is None:
        raise ManifestError(
            source, f"unknown or missing kind {kind!r} (capability|tool|agent|skill)"
        )
    try:
        parsed = model.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]
        )
        raise ManifestError(source, details) from exc
    return parsed  # type: ignore[return-value]


@dataclass
class LoadResult:
    manifests: list[
        tuple[str, CapabilityManifest | ToolManifest | AgentManifest | SkillManifest]
    ] = field(default_factory=list)
    errors: list[ManifestError] = field(default_factory=list)


def load_directory(directory: Path) -> LoadResult:
    """Parse every ``*.yaml``/``*.yml`` file (multi-document allowed) under ``directory``.

    Invalid documents are reported in ``errors`` and skipped; they are never partially applied.
    Symlinks are not followed; oversized files are rejected.
    """
    result = LoadResult()
    if not directory.is_dir():
        return result
    for path in sorted(directory.rglob("*")):
        if path.suffix not in {".yaml", ".yml"} or path.is_symlink() or not path.is_file():
            continue
        label = str(path.relative_to(directory))
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            result.errors.append(ManifestError(label, f"file exceeds {MAX_MANIFEST_BYTES} bytes"))
            continue
        try:
            docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except (yaml.YAMLError, UnicodeDecodeError) as exc:
            result.errors.append(ManifestError(label, f"invalid YAML: {str(exc)[:200]}"))
            continue
        for index, doc in enumerate(d for d in docs if d is not None):
            source = f"{label}#{index}"
            try:
                result.manifests.append((source, parse_document(doc, source)))
            except ManifestError as exc:
                result.errors.append(exc)
    return result
