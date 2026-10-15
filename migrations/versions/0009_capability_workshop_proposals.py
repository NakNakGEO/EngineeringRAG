"""Capability Workshop proposals and history (Phase 10).

Downgrade drops the workshop schema and ALL proposals (registered artifacts stay registered).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-06 13:02:08.800537+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0009'
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS workshop")
    op.create_table('proposal',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('version', sa.Text(), nullable=False),
    sa.Column('state', sa.Text(), nullable=False),
    sa.Column('spec', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('spec_hash', sa.Text(), nullable=False),
    sa.Column('source', sa.Text(), nullable=True),
    sa.Column('source_sha256', sa.Text(), nullable=True),
    sa.Column('creator', sa.Text(), nullable=False),
    sa.Column('creator_kind', sa.Text(), nullable=False),
    sa.Column('prompt_hash', sa.Text(), nullable=True),
    sa.Column('need', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('capability_claims', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('tests', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('test_results', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('scan_report', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('approval', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('registered_ref', sa.Text(), nullable=True),
    sa.Column('artifact_path', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("creator_kind in ('llm','human','system')", name=op.f('ck_proposal_creator_kind_valid')),
    sa.CheckConstraint("kind in ('skill', 'agent', 'tool')", name=op.f('ck_proposal_kind_valid')),
    sa.CheckConstraint("name !~ '^external_database'", name=op.f('ck_proposal_not_external_database')),
    sa.CheckConstraint("state in ('DRAFT', 'SANDBOXED', 'TESTED', 'EXPERIMENTAL', 'VERIFIED', 'TRUSTED', 'REJECTED')", name=op.f('ck_proposal_state_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_proposal')),
    schema='workshop'
    )
    op.create_index('ix_proposal_name', 'proposal', ['kind', 'name', 'version'], unique=False, schema='workshop')
    op.create_index('ix_proposal_state', 'proposal', ['state', 'created_at'], unique=False, schema='workshop')
    op.create_table('history',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('proposal_id', sa.UUID(), nullable=False),
    sa.Column('from_state', sa.Text(), nullable=True),
    sa.Column('to_state', sa.Text(), nullable=False),
    sa.Column('actor', sa.Text(), nullable=False),
    sa.Column('reason', sa.Text(), server_default='', nullable=False),
    sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['proposal_id'], ['workshop.proposal.id'], name='fk_history_proposal', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_history')),
    schema='workshop'
    )
    op.create_index('ix_workshop_history_proposal', 'history', ['proposal_id', 'at'], unique=False, schema='workshop')


def downgrade() -> None:
    op.drop_index('ix_workshop_history_proposal', table_name='history', schema='workshop')
    op.drop_table('history', schema='workshop')
    op.drop_index('ix_proposal_state', table_name='proposal', schema='workshop')
    op.drop_index('ix_proposal_name', table_name='proposal', schema='workshop')
    op.drop_table('proposal', schema='workshop')
    op.execute("DROP SCHEMA IF EXISTS workshop")
