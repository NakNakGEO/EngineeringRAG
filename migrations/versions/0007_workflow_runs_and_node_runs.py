"""Workflow runs and node runs (Phase 7).

Downgrade drops the workflow schema and ALL workflow state.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-06 12:29:04.566335+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS workflow")
    op.create_table('run',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('run_id', sa.UUID(), nullable=False),
    sa.Column('definition_id', sa.Text(), nullable=False),
    sa.Column('definition_version', sa.Text(), nullable=False),
    sa.Column('definition', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('goal', sa.Text(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=True),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('current_node', sa.Text(), nullable=True),
    sa.Column('risk', sa.Text(), nullable=False),
    sa.Column('team', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('steps', sa.Integer(), server_default='0', nullable=False),
    sa.Column('version', sa.Integer(), server_default='0', nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("(status IN ('completed','failed','cancelled')) = (finished_at IS NOT NULL)", name=op.f('ck_run_finished_matches_status')),
    sa.CheckConstraint("risk in ('low','medium','high','critical')", name=op.f('ck_run_risk_valid')),
    sa.CheckConstraint("status in ('running', 'waiting_approval', 'completed', 'failed', 'cancelled')", name=op.f('ck_run_status_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_run')),
    schema='workflow'
    )
    op.create_index('ix_workflow_run_run', 'run', ['run_id'], unique=False, schema='workflow')
    op.create_index('ix_workflow_run_status', 'run', ['status', 'created_at'], unique=False, schema='workflow')
    op.create_table('node_run',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('workflow_id', sa.UUID(), nullable=False),
    sa.Column('node_id', sa.Text(), nullable=False),
    sa.Column('stage', sa.Text(), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reporter', sa.Text(), nullable=True),
    sa.Column('outputs', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('criteria', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('approval_id', sa.UUID(), nullable=True),
    sa.CheckConstraint("status in ('active', 'waiting_approval', 'passed', 'failed', 'skipped')", name=op.f('ck_node_run_status_valid')),
    sa.CheckConstraint('attempt >= 1', name=op.f('ck_node_run_attempt_positive')),
    sa.ForeignKeyConstraint(['workflow_id'], ['workflow.run.id'], name='fk_node_run_workflow', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_node_run')),
    sa.UniqueConstraint('workflow_id', 'node_id', 'attempt', name='uq_node_attempt'),
    schema='workflow'
    )
    op.create_index('ix_node_run_workflow', 'node_run', ['workflow_id', 'started_at'], unique=False, schema='workflow')


def downgrade() -> None:
    op.drop_index('ix_node_run_workflow', table_name='node_run', schema='workflow')
    op.drop_table('node_run', schema='workflow')
    op.drop_index('ix_workflow_run_status', table_name='run', schema='workflow')
    op.drop_index('ix_workflow_run_run', table_name='run', schema='workflow')
    op.drop_table('run', schema='workflow')
    op.execute("DROP SCHEMA IF EXISTS workflow")
