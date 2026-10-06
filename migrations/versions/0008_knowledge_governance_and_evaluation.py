"""Knowledge governance (lifecycle, dependencies, contradictions) and evaluation runs (Phase 9).

Downgrade drops those tables and ALL their history; knowledge items themselves are kept.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-06 12:45:00.870286+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS evaluation")
    op.create_table('run',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('subject', sa.Text(), nullable=False),
    sa.Column('subject_version', sa.Text(), nullable=True),
    sa.Column('capability_id', sa.Text(), nullable=True),
    sa.Column('score', sa.Float(), nullable=True),
    sa.Column('samples', sa.Integer(), server_default='0', nullable=False),
    sa.Column('passed', sa.Integer(), server_default='0', nullable=False),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('evaluator', sa.Text(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('passed >= 0 AND passed <= samples', name=op.f('ck_run_passed_range')),
    sa.CheckConstraint('score IS NULL OR (score >= 0 AND score <= 1)', name=op.f('ck_run_score_range')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_run')),
    schema='evaluation'
    )
    op.create_index('ix_eval_subject', 'run', ['subject', 'started_at'], unique=False, schema='evaluation')
    op.create_table('lifecycle',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('subject_kind', sa.Text(), nullable=False),
    sa.Column('subject_id', sa.UUID(), nullable=False),
    sa.Column('change', sa.Text(), nullable=False),
    sa.Column('from_value', sa.Text(), nullable=True),
    sa.Column('to_value', sa.Text(), nullable=False),
    sa.Column('actor', sa.Text(), nullable=False),
    sa.Column('reason', sa.Text(), server_default='', nullable=False),
    sa.Column('evidence_ids', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('approval', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("change in ('trust', 'health', 'supersede', 'contradiction', 'status', 'create')", name=op.f('ck_lifecycle_change_valid')),
    sa.CheckConstraint("subject_kind in ('item', 'decision')", name=op.f('ck_lifecycle_subject_kind_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lifecycle')),
    schema='knowledge'
    )
    op.create_index('ix_lifecycle_subject', 'lifecycle', ['subject_kind', 'subject_id', 'at'], unique=False, schema='knowledge')
    op.create_table('contradiction',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('item_a', sa.UUID(), nullable=False),
    sa.Column('item_b', sa.UUID(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('detected_by', sa.Text(), nullable=False),
    sa.Column('resolved_by', sa.Text(), nullable=True),
    sa.Column('resolution', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status in ('open','resolved')", name=op.f('ck_contradiction_status_valid')),
    sa.CheckConstraint('item_a <> item_b', name=op.f('ck_contradiction_distinct_items')),
    sa.ForeignKeyConstraint(['item_a'], ['knowledge.item.id'], name='fk_contradiction_a', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['item_b'], ['knowledge.item.id'], name='fk_contradiction_b', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_contradiction')),
    sa.UniqueConstraint('item_a', 'item_b', name='uq_contradiction_pair'),
    schema='knowledge'
    )
    op.create_index('ix_contradiction_status', 'contradiction', ['status'], unique=False, schema='knowledge')
    op.create_table('dependency',
    sa.Column('item_id', sa.UUID(), nullable=False),
    sa.Column('dep_kind', sa.Text(), nullable=False),
    sa.Column('dep_key', sa.Text(), nullable=False),
    sa.Column('dep_hash', sa.Text(), nullable=True),
    sa.Column('project_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("dep_kind in ('file', 'symbol', 'item')", name=op.f('ck_dependency_dep_kind_valid')),
    sa.ForeignKeyConstraint(['item_id'], ['knowledge.item.id'], name='fk_dependency_item', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('item_id', 'dep_kind', 'dep_key', name='pk_dependency'),
    schema='knowledge'
    )
    op.create_index('ix_dependency_lookup', 'dependency', ['project_id', 'dep_kind', 'dep_key'], unique=False, schema='knowledge')


def downgrade() -> None:
    op.drop_index('ix_dependency_lookup', table_name='dependency', schema='knowledge')
    op.drop_table('dependency', schema='knowledge')
    op.drop_index('ix_contradiction_status', table_name='contradiction', schema='knowledge')
    op.drop_table('contradiction', schema='knowledge')
    op.drop_index('ix_lifecycle_subject', table_name='lifecycle', schema='knowledge')
    op.drop_table('lifecycle', schema='knowledge')
    op.drop_index('ix_eval_subject', table_name='run', schema='evaluation')
    op.drop_table('run', schema='evaluation')
    op.execute("DROP SCHEMA IF EXISTS evaluation")
