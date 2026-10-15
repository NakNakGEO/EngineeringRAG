"""Runtime settings (the local language model chosen in the UI).

Adds ``platform.setting`` (key / JSON value). Holds no secrets.

Downgrade drops ``platform.setting``; the values from ``.env`` apply again.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-15 15:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0012'
down_revision: str | None = '0011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('setting',
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('key', name=op.f('pk_setting')),
    schema='platform'
    )


def downgrade() -> None:
    op.drop_table('setting', schema='platform')
