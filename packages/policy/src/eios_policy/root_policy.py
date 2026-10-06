"""Root Policy: loaded once from a hash-pinned file, immutable thereafter.

Defence in depth: the structural invariants (external databases are denied, the forbidden
capability list) are *also* compiled in here, so even a file that was edited and re-locked cannot
weaken them - the loader merges the compiled-in minimums into whatever the file says.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from eios_domain.policy import Risk
from eios_domain.registry import FORBIDDEN_CAPABILITIES, FORBIDDEN_CAPABILITY_PREFIXES

LOCK_SUFFIX = ".lock"
MAX_POLICY_BYTES = 200_000

# Compiled-in minimums (cannot be removed by the file).
MIN_DATABASE_CLIENTS = frozenset(
    {"psql", "mysql", "mariadb", "sqlcmd", "sqlplus", "mongosh", "mongo", "redis-cli", "bcp"}
)
MIN_DATABASE_PORTS = frozenset({1433, 1521, 3306, 5432, 27017, 6379})
MIN_APPROVAL_ACTIONS = frozenset(
    {"git.push", "git.merge", "git.rebase", "git.reset_hard", "git.release", "git.force_push",
     "git.history_rewrite"}
)  # fmt: skip


class RootPolicyError(Exception):
    """The Root Policy file is missing, unreadable, invalid or does not match its pinned digest."""


class RootPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1, max_length=40)
    description: str = ""
    forbidden_capabilities: frozenset[str] = frozenset()
    forbidden_capability_prefixes: tuple[str, ...] = ()
    database_client_binaries: frozenset[str] = frozenset()
    database_ports: frozenset[int] = frozenset()
    approval_required_actions: frozenset[str] = frozenset()
    denied_path_fragments: tuple[str, ...] = ()
    denied_file_names: frozenset[str] = frozenset()
    internal_hosts: frozenset[str] = frozenset()
    experimental_approval_risk: Risk = Risk.HIGH
    digest: str = Field(default="", description="sha256 of the canonical policy document")

    @field_validator("database_ports")
    @classmethod
    def _ports(cls, v: frozenset[int]) -> frozenset[int]:
        if any(not 0 < p < 65536 for p in v):
            raise ValueError("invalid port")
        return v

    def is_forbidden_capability(self, capability: str) -> bool:
        lowered = capability.lower()
        return lowered in self.forbidden_capabilities or lowered.startswith(
            self.forbidden_capability_prefixes
        )

    def summary(self) -> dict[str, object]:
        return {
            "version": self.version,
            "digest": self.digest,
            "forbidden_capabilities": sorted(self.forbidden_capabilities),
            "approval_required_actions": sorted(self.approval_required_actions),
            "database_client_binaries": sorted(self.database_client_binaries),
            "database_ports": sorted(self.database_ports),
            "mutable_at_runtime": False,
        }


def canonical_digest(document: dict[str, object]) -> str:
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def lock_path(policy_path: Path) -> Path:
    return policy_path.with_name(policy_path.stem + LOCK_SUFFIX)


def load_root_policy(path: Path) -> RootPolicy:
    """Load and verify the policy. Any problem raises :class:`RootPolicyError` (fail closed)."""
    try:
        if path.is_symlink() or not path.is_file():
            raise RootPolicyError(f"root policy {path} is missing or not a regular file")
        if path.stat().st_size > MAX_POLICY_BYTES:
            raise RootPolicyError("root policy file is too large")
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        pinned = lock_path(path).read_text(encoding="utf-8").strip()
    except (OSError, yaml.YAMLError, UnicodeDecodeError) as exc:
        raise RootPolicyError(f"root policy unreadable: {exc}") from exc
    if not isinstance(document, dict):
        raise RootPolicyError("root policy must be a mapping")
    digest = canonical_digest(document)
    if digest != pinned:
        raise RootPolicyError(
            "root policy digest does not match root_policy.lock "
            f"(expected {pinned[:12]}..., got {digest[:12]}...); refusing to start"
        )
    try:
        loaded = RootPolicy.model_validate({**document, "digest": digest})
    except ValidationError as exc:
        raise RootPolicyError(f"invalid root policy: {exc.errors()[0]['msg']}") from exc
    return loaded.model_copy(
        update={
            "forbidden_capabilities": loaded.forbidden_capabilities | FORBIDDEN_CAPABILITIES,
            "forbidden_capability_prefixes": tuple(
                sorted(
                    set(loaded.forbidden_capability_prefixes) | set(FORBIDDEN_CAPABILITY_PREFIXES)
                )
            ),
            "database_client_binaries": loaded.database_client_binaries | MIN_DATABASE_CLIENTS,
            "database_ports": loaded.database_ports | MIN_DATABASE_PORTS,
            "approval_required_actions": loaded.approval_required_actions | MIN_APPROVAL_ACTIONS,
        }
    )


def write_lock(path: Path) -> str:
    """Release tooling only: (re)write the lock for a reviewed policy file."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    digest = canonical_digest(document)
    lock_path(path).write_text(digest + "\n", encoding="utf-8")
    return digest


if __name__ == "__main__":  # pragma: no cover - release tooling
    print(write_lock(Path(sys.argv[1])))
