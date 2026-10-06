"""Shared column/constraint builders for the vault-classified tables."""

from __future__ import annotations

import sqlalchemy as sa

# Dimensionality of stored embeddings. The default local embedder produces this many dimensions.
# Changing it requires a new embedding column + re-embed migration (see ADR 0007).
EMBEDDING_DIM = 256

VAULTS = ("default", "project", "ephemeral")
TRUST_LEVELS = ("RAW", "OBSERVED", "DERIVED", "VERIFIED", "APPROVED")
HEALTH_STATES = (
    "CURRENT", "UNVERIFIED", "STALE", "CONTRADICTED", "SUPERSEDED", "HISTORICAL", "QUARANTINED",
)  # fmt: skip
SOURCE_KINDS = ("manual", "tool", "llm", "code", "research", "import")


def in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} in ({', '.join(repr(v) for v in values)})"


def vault_checks(*, expires: bool = True) -> list[sa.CheckConstraint]:
    """DB-level mirror of ``eios_domain.vault.check_vault_invariants`` (defence in depth)."""
    checks = [
        sa.CheckConstraint(in_list("vault", VAULTS), name="vault_valid"),
        sa.CheckConstraint("vault <> 'project' OR project_id IS NOT NULL", name="project_needs_id"),
        sa.CheckConstraint("vault <> 'default' OR project_id IS NULL", name="default_no_project"),
    ]
    if expires:
        checks.append(
            sa.CheckConstraint(
                "vault <> 'ephemeral' OR expires_at IS NOT NULL", name="ephemeral_expires"
            )
        )
    return checks
