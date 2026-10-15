"""Tool onboarding candidates (repositories downloaded to be inspected, never run).

Adds ``platform.tool_candidate``: source, ref, archive hash/size, the static-inspection report and
the human decision. The archive itself is not stored.

Downgrade drops ``platform.tool_candidate``.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-15 18:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0013'
down_revision: str | None = '0012'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('tool_candidate',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('source_url', sa.Text(), nullable=False),
    sa.Column('ref', sa.Text(), nullable=False),
    sa.Column('archive_sha256', sa.Text(), nullable=False),
    sa.Column('archive_bytes', sa.Integer(), nullable=False),
    sa.Column('state', sa.Text(), nullable=False),
    sa.Column('report', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('requested_by', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('decided_by', sa.Text(), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('note', sa.Text(), server_default='', nullable=False),
    sa.CheckConstraint("state in ('quarantined','reviewed','rejected')", name=op.f('ck_tool_candidate_state_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tool_candidate')),
    schema='platform'
    )
    op.create_index('ix_tool_candidate_created_at', 'tool_candidate', ['created_at'], unique=False, schema='platform')


def downgrade() -> None:
    op.drop_index('ix_tool_candidate_created_at', table_name='tool_candidate', schema='platform')
    op.drop_table('tool_candidate', schema='platform')
