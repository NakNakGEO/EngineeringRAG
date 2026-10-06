"""Baseline: empty schema.

Phase 0 creates no domain tables. Each later phase adds its own migrations (and the PostgreSQL
schema it owns). This revision exists so the migration chain, `alembic_version` bookkeeping,
upgrade and downgrade paths are exercised from day one.

Revision ID: 0001
Revises:
Create Date: 2026-10-06 00:00:00+00:00
"""

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
