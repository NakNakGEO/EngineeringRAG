"""Vault classification: where data lives decides whether it may ever leave the machine."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from eios_domain.ids import utcnow


class Vault(StrEnum):
    DEFAULT = "default"  # portable personal/public/generic knowledge
    PROJECT = "project"  # local-only project/company knowledge; never exported by default
    EPHEMERAL = "ephemeral"  # temporary; must expire

    @property
    def exportable(self) -> bool:
        """Only the Default Vault may be part of a portable export."""
        return self is Vault.DEFAULT


class VaultViolationError(ValueError):
    """Data was classified inconsistently with its vault."""


def check_vault_invariants(
    vault: Vault,
    *,
    project_id: uuid.UUID | None,
    expires_at: datetime | None,
    now: datetime | None = None,
) -> None:
    """Raise :class:`VaultViolationError` unless (vault, project, expiry) are consistent.

    * PROJECT data must name its project.
    * DEFAULT data must not be tied to a project (that would be company data in a portable vault).
    * EPHEMERAL data must carry an expiry in the future.
    """
    if vault is Vault.PROJECT and project_id is None:
        raise VaultViolationError("project vault data requires a project_id")
    if vault is Vault.DEFAULT and project_id is not None:
        raise VaultViolationError("default vault data must not belong to a project")
    if vault is Vault.EPHEMERAL:
        if expires_at is None:
            raise VaultViolationError("ephemeral data requires expires_at")
        if expires_at <= (now or utcnow()):
            raise VaultViolationError("ephemeral expires_at must be in the future")
    elif expires_at is not None and vault is Vault.DEFAULT:
        raise VaultViolationError("default vault data does not expire")
