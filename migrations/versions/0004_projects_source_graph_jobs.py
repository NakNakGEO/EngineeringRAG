"""Projects, source files/symbols, code graph and the job queue (Phase 3).

Creates schemas ``project``, ``source``, ``graph`` and tables ``platform.job``, ``project.*``,
``source.file``, ``source.symbol``, ``graph.node``, ``graph.edge``.

Downgrade drops these tables and ALL project index data and queued jobs.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-06 11:11:11.714061+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for schema in ("project", "source", "graph"):
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

    op.create_table('edge',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=False),
    sa.Column('branch', sa.Text(), nullable=False),
    sa.Column('scope', sa.Text(), nullable=False),
    sa.Column('src_key', sa.Text(), nullable=False),
    sa.Column('dst_key', sa.Text(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('owner_path', sa.Text(), nullable=True),
    sa.Column('weight', sa.Float(), server_default='1', nullable=False),
    sa.Column('confidence', sa.Float(), server_default='1', nullable=False),
    sa.Column('attrs', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.CheckConstraint("scope in ('committed','overlay')", name=op.f('ck_edge_scope_valid')),
    sa.CheckConstraint('confidence >= 0 AND confidence <= 1', name=op.f('ck_edge_confidence_range')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_edge')),
    sa.UniqueConstraint('project_id', 'branch', 'scope', 'src_key', 'dst_key', 'kind', name='uq_edge'),
    schema='graph'
    )
    op.create_index('ix_edge_dst', 'edge', ['project_id', 'branch', 'scope', 'dst_key'], unique=False, schema='graph')
    op.create_index('ix_edge_owner', 'edge', ['project_id', 'branch', 'scope', 'owner_path'], unique=False, schema='graph')
    op.create_index('ix_edge_src', 'edge', ['project_id', 'branch', 'scope', 'src_key'], unique=False, schema='graph')
    op.create_table('node',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=False),
    sa.Column('branch', sa.Text(), nullable=False),
    sa.Column('scope', sa.Text(), nullable=False),
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('label', sa.Text(), nullable=False),
    sa.Column('path', sa.Text(), nullable=True),
    sa.Column('attrs', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.CheckConstraint("scope in ('committed','overlay')", name=op.f('ck_node_scope_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_node')),
    sa.UniqueConstraint('project_id', 'branch', 'scope', 'key', name='uq_node_key'),
    schema='graph'
    )
    op.create_index('ix_node_kind', 'node', ['project_id', 'branch', 'scope', 'kind'], unique=False, schema='graph')
    op.create_index('ix_node_path', 'node', ['project_id', 'branch', 'scope', 'path'], unique=False, schema='graph')
    op.create_table('job',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('type', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('lease_owner', sa.Text(), nullable=True),
    sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('max_attempts', sa.Integer(), server_default='3', nullable=False),
    sa.Column('available_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('idempotency_key', sa.Text(), nullable=True),
    sa.Column('run_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("status in ('queued','running','succeeded','failed','dead')", name=op.f('ck_job_status_valid')),
    sa.CheckConstraint('attempts >= 0 AND max_attempts >= 1', name=op.f('ck_job_attempts_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_job')),
    schema='platform'
    )
    op.create_index('ix_job_claim', 'job', ['status', 'available_at'], unique=False, schema='platform')
    op.create_index('uq_job_idempotency', 'job', ['type', 'idempotency_key'], unique=True, schema='platform', postgresql_where=sa.text('idempotency_key IS NOT NULL'))
    op.create_table('project',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('fingerprint', sa.Text(), nullable=False),
    sa.Column('remote', sa.Text(), nullable=True),
    sa.Column('root_commit', sa.Text(), nullable=True),
    sa.Column('local_root', sa.Text(), nullable=False),
    sa.Column('default_branch', sa.Text(), nullable=True),
    sa.Column('bootstrap_state', sa.Text(), nullable=False),
    sa.Column('last_branch', sa.Text(), nullable=True),
    sa.Column('last_commit', sa.Text(), nullable=True),
    sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.CheckConstraint("bootstrap_state in ('NEW', 'CURRENT', 'STALE', 'DIRTY', 'BRANCH_CHANGED', 'MAJOR_DIVERGENCE', 'ERROR')", name=op.f('ck_project_state_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_project')),
    sa.UniqueConstraint('fingerprint', name='uq_project_fingerprint'),
    schema='project'
    )
    op.create_table('location',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=False),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['project.project.id'], name=op.f('fk_location_project_id_project'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_location')),
    sa.UniqueConstraint('project_id', 'path', name='uq_location_project_path'),
    schema='project'
    )
    op.create_table('snapshot',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=False),
    sa.Column('branch', sa.Text(), nullable=False),
    sa.Column('commit_sha', sa.Text(), nullable=False),
    sa.Column('dirty', sa.Boolean(), nullable=False),
    sa.Column('dirty_paths', sa.Integer(), server_default='0', nullable=False),
    sa.Column('state', sa.Text(), nullable=False),
    sa.Column('files_total', sa.Integer(), server_default='0', nullable=False),
    sa.Column('files_changed', sa.Integer(), server_default='0', nullable=False),
    sa.Column('symbols_total', sa.Integer(), server_default='0', nullable=False),
    sa.Column('duration_ms', sa.Integer(), server_default='0', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("state in ('NEW', 'CURRENT', 'STALE', 'DIRTY', 'BRANCH_CHANGED', 'MAJOR_DIVERGENCE', 'ERROR')", name=op.f('ck_snapshot_state_valid')),
    sa.ForeignKeyConstraint(['project_id'], ['project.project.id'], name=op.f('fk_snapshot_project_id_project'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_snapshot')),
    schema='project'
    )
    op.create_index('ix_snapshot_project_created', 'snapshot', ['project_id', 'created_at'], unique=False, schema='project')
    op.create_table('file',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=False),
    sa.Column('branch', sa.Text(), nullable=False),
    sa.Column('scope', sa.Text(), nullable=False),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('language', sa.Text(), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('line_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('content_hash', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), server_default='active', nullable=False),
    sa.Column('indexed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.CheckConstraint("(scope = 'overlay' AND expires_at IS NOT NULL) OR (scope = 'committed' AND expires_at IS NULL)", name=op.f('ck_file_overlay_expires')),
    sa.CheckConstraint("scope in ('committed','overlay')", name=op.f('ck_file_scope_valid')),
    sa.CheckConstraint("status in ('active','deleted')", name=op.f('ck_file_status_valid')),
    sa.ForeignKeyConstraint(['project_id'], ['project.project.id'], name=op.f('fk_file_project_id_project'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_file')),
    sa.UniqueConstraint('project_id', 'branch', 'scope', 'path', name='uq_file_location'),
    schema='source'
    )
    op.create_index('ix_file_expires_at', 'file', ['expires_at'], unique=False, schema='source', postgresql_where=sa.text('expires_at IS NOT NULL'))
    op.create_index('ix_file_project_branch_scope', 'file', ['project_id', 'branch', 'scope'], unique=False, schema='source')
    op.create_table('symbol',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('file_id', sa.UUID(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=False),
    sa.Column('branch', sa.Text(), nullable=False),
    sa.Column('scope', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('name_lower', sa.Text(), sa.Computed('lower(name)', persisted=True), nullable=True),
    sa.Column('qualified_name', sa.Text(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('container', sa.Text(), nullable=True),
    sa.Column('start_line', sa.Integer(), nullable=False),
    sa.Column('end_line', sa.Integer(), nullable=False),
    sa.Column('signature', sa.Text(), server_default='', nullable=False),
    sa.Column('exported', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.ForeignKeyConstraint(['file_id'], ['source.file.id'], name=op.f('fk_symbol_file_id_file'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_symbol')),
    schema='source'
    )
    op.create_index('ix_symbol_file', 'symbol', ['file_id'], unique=False, schema='source')
    op.create_index('ix_symbol_name', 'symbol', ['project_id', 'branch', 'scope', 'name_lower'], unique=False, schema='source')


def downgrade() -> None:
    op.drop_index('ix_symbol_name', table_name='symbol', schema='source')
    op.drop_index('ix_symbol_file', table_name='symbol', schema='source')
    op.drop_table('symbol', schema='source')
    op.drop_index('ix_file_project_branch_scope', table_name='file', schema='source')
    op.drop_index('ix_file_expires_at', table_name='file', schema='source', postgresql_where=sa.text('expires_at IS NOT NULL'))
    op.drop_table('file', schema='source')
    op.drop_index('ix_snapshot_project_created', table_name='snapshot', schema='project')
    op.drop_table('snapshot', schema='project')
    op.drop_table('location', schema='project')
    op.drop_table('project', schema='project')
    op.drop_index('uq_job_idempotency', table_name='job', schema='platform', postgresql_where=sa.text('idempotency_key IS NOT NULL'))
    op.drop_index('ix_job_claim', table_name='job', schema='platform')
    op.drop_table('job', schema='platform')
    op.drop_index('ix_node_path', table_name='node', schema='graph')
    op.drop_index('ix_node_kind', table_name='node', schema='graph')
    op.drop_table('node', schema='graph')
    op.drop_index('ix_edge_src', table_name='edge', schema='graph')
    op.drop_index('ix_edge_owner', table_name='edge', schema='graph')
    op.drop_index('ix_edge_dst', table_name='edge', schema='graph')
    op.drop_table('edge', schema='graph')
    for schema in ("graph", "source", "project"):
        op.execute(f"DROP SCHEMA IF EXISTS {schema}")
