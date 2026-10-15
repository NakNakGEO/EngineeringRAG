"""Short model-written file summaries, one per file and version.

Adds ``project.file_summary``. A row holds the three-line summary of one file for one content
hash; when the file changes the row is replaced by a fresh summary.

Downgrade drops ``project.file_summary`` (summaries are regenerated on demand).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-15 12:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0011'
down_revision: str | None = '0010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('file_summary',
    sa.Column('project_id', sa.UUID(), nullable=False),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('content_hash', sa.Text(), nullable=False),
    sa.Column('summary', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('provider', sa.Text(), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('input_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('output_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['project.project.id'], name=op.f('fk_file_summary_project_id_project'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('project_id', 'path', name=op.f('pk_file_summary')),
    schema='project'
    )


def downgrade() -> None:
    op.drop_table('file_summary', schema='project')
