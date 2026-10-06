"""Search scope: which vaults and projects a query may see. Isolation is enforced in SQL."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import sqlalchemy as sa

from eios_domain.knowledge import Health
from eios_domain.vault import Vault


@dataclass(frozen=True)
class SearchScope:
    """``project_id`` selects the one Project Vault that is visible (None = no project data).

    The Default Vault is visible unless excluded. Another project's data is never visible.
    """

    project_id: uuid.UUID | None = None
    vaults: frozenset[Vault] = frozenset({Vault.DEFAULT, Vault.PROJECT, Vault.EPHEMERAL})
    exclude_health: frozenset[Health] = field(
        default_factory=lambda: frozenset({Health.QUARANTINED, Health.SUPERSEDED})
    )
    include_expired: bool = False


def scope_clause(
    table: sa.Table, scope: SearchScope, *, has_health: bool = True
) -> sa.ColumnElement[bool]:
    """SQL predicate implementing :class:`SearchScope` for a vault-classified table."""
    vault_ok: list[sa.ColumnElement[bool]] = []
    if Vault.DEFAULT in scope.vaults:
        vault_ok.append(table.c.vault == Vault.DEFAULT.value)
    if Vault.PROJECT in scope.vaults and scope.project_id is not None:
        vault_ok.append(
            sa.and_(table.c.vault == Vault.PROJECT.value, table.c.project_id == scope.project_id)
        )
    if Vault.EPHEMERAL in scope.vaults:
        project_match = (
            table.c.project_id.is_(None)
            if scope.project_id is None
            else sa.or_(table.c.project_id.is_(None), table.c.project_id == scope.project_id)
        )
        vault_ok.append(sa.and_(table.c.vault == Vault.EPHEMERAL.value, project_match))
    clause: sa.ColumnElement[bool] = sa.or_(*vault_ok) if vault_ok else sa.false()
    if has_health and scope.exclude_health:
        clause = sa.and_(clause, table.c.health.notin_([h.value for h in scope.exclude_health]))
    if not scope.include_expired and "expires_at" in table.c:
        clause = sa.and_(
            clause, sa.or_(table.c.expires_at.is_(None), table.c.expires_at > sa.func.now())
        )
    return clause
