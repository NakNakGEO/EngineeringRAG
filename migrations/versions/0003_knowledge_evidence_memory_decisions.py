"""Knowledge, evidence, memory and the decision ledger (Phase 2).

Enables pgvector; creates schemas ``evidence``, ``knowledge``, ``memory`` and their tables,
including full-text (GIN) and vector (HNSW, cosine) indexes and DB-level vault CHECK constraints.

Downgrade drops these tables and ALL knowledge, evidence and memory data.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-06 11:01:17.609417+00:00
"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    for schema in ("evidence", "knowledge", "memory"):
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

    op.create_table('blob',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('sha256', sa.Text(), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('storage_path', sa.Text(), nullable=False),
    sa.Column('media_type', sa.Text(), server_default='application/octet-stream', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('size_bytes >= 0', name=op.f('ck_blob_size_nonnegative')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_blob')),
    sa.UniqueConstraint('sha256', name='uq_blob_sha256'),
    schema='evidence'
    )
    op.create_table('finding',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('observation_ids', postgresql.ARRAY(sa.UUID()), nullable=False),
    sa.Column('statement', sa.Text(), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('limitations', sa.Text(), server_default='', nullable=False),
    sa.Column('created_by', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('cardinality(observation_ids) >= 1', name=op.f('ck_finding_needs_observation')),
    sa.CheckConstraint('confidence >= 0 AND confidence <= 1', name=op.f('ck_finding_confidence_range')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_finding')),
    schema='evidence'
    )
    op.create_table('decision',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('vault', sa.Text(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=True),
    sa.Column('question', sa.Text(), nullable=False),
    sa.Column('alternatives', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('selected', sa.Text(), server_default='', nullable=False),
    sa.Column('rationale', sa.Text(), server_default='', nullable=False),
    sa.Column('evidence_ids', postgresql.ARRAY(sa.UUID()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('decider', sa.Text(), nullable=False),
    sa.Column('reviewers', postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('superseded_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('search_vector', postgresql.TSVECTOR(), sa.Computed("to_tsvector('english', question || ' ' || selected || ' ' || rationale)", persisted=True), nullable=True),
    sa.CheckConstraint("status in ('proposed','accepted','rejected','superseded')", name=op.f('ck_decision_status_valid')),
    sa.CheckConstraint("vault <> 'default' OR project_id IS NULL", name=op.f('ck_decision_default_no_project')),
    sa.CheckConstraint("vault <> 'ephemeral'", name=op.f('ck_decision_durable_vault')),
    sa.CheckConstraint("vault <> 'project' OR project_id IS NOT NULL", name=op.f('ck_decision_project_needs_id')),
    sa.CheckConstraint("vault in ('default', 'project', 'ephemeral')", name=op.f('ck_decision_vault_valid')),
    sa.ForeignKeyConstraint(['superseded_by'], ['knowledge.decision.id'], name=op.f('fk_decision_superseded_by_decision'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_decision')),
    schema='knowledge'
    )
    op.create_index('ix_decision_search_vector', 'decision', ['search_vector'], unique=False, schema='knowledge', postgresql_using='gin')
    op.create_index('ix_decision_vault_project', 'decision', ['vault', 'project_id'], unique=False, schema='knowledge')
    op.create_table('item',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('vault', sa.Text(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=True),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('tags', postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('trust', sa.Text(), nullable=False),
    sa.Column('health', sa.Text(), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('source_kind', sa.Text(), nullable=False),
    sa.Column('created_by', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('subject_key', sa.Text(), nullable=True),
    sa.Column('limitations', sa.Text(), server_default='', nullable=False),
    sa.Column('version_ref', sa.Text(), nullable=True),
    sa.Column('superseded_by', sa.UUID(), nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=256), nullable=True),
    sa.Column('embedding_model', sa.Text(), nullable=True),
    sa.Column('search_vector', postgresql.TSVECTOR(), sa.Computed("to_tsvector('english', title || ' ' || content)", persisted=True), nullable=True),
    sa.CheckConstraint("health in ('CURRENT', 'UNVERIFIED', 'STALE', 'CONTRADICTED', 'SUPERSEDED', 'HISTORICAL', 'QUARANTINED')", name=op.f('ck_item_health_valid')),
    sa.CheckConstraint("source_kind in ('manual', 'tool', 'llm', 'code', 'research', 'import')", name=op.f('ck_item_source_kind_valid')),
    sa.CheckConstraint("trust in ('RAW', 'OBSERVED', 'DERIVED', 'VERIFIED', 'APPROVED')", name=op.f('ck_item_trust_valid')),
    sa.CheckConstraint("vault <> 'default' OR project_id IS NULL", name=op.f('ck_item_default_no_project')),
    sa.CheckConstraint("vault <> 'ephemeral' OR expires_at IS NOT NULL", name=op.f('ck_item_ephemeral_expires')),
    sa.CheckConstraint("vault <> 'project' OR project_id IS NOT NULL", name=op.f('ck_item_project_needs_id')),
    sa.CheckConstraint("vault in ('default', 'project', 'ephemeral')", name=op.f('ck_item_vault_valid')),
    sa.CheckConstraint('confidence >= 0 AND confidence <= 1', name=op.f('ck_item_confidence_range')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_item')),
    schema='knowledge'
    )
    op.create_index('ix_item_embedding', 'item', ['embedding'], unique=False, schema='knowledge', postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'})
    op.create_index('ix_item_expires_at', 'item', ['expires_at'], unique=False, schema='knowledge', postgresql_where=sa.text('expires_at IS NOT NULL'))
    op.create_index('ix_item_health', 'item', ['health'], unique=False, schema='knowledge')
    op.create_index('ix_item_search_vector', 'item', ['search_vector'], unique=False, schema='knowledge', postgresql_using='gin')
    op.create_index('ix_item_subject_key', 'item', ['subject_key'], unique=False, schema='knowledge')
    op.create_index('ix_item_vault_project', 'item', ['vault', 'project_id'], unique=False, schema='knowledge')
    op.create_table('item',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('vault', sa.Text(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=True),
    sa.Column('scope', sa.Text(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('tags', postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('importance', sa.Float(), nullable=False),
    sa.Column('created_by', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=256), nullable=True),
    sa.Column('embedding_model', sa.Text(), nullable=True),
    sa.Column('search_vector', postgresql.TSVECTOR(), sa.Computed("to_tsvector('english', content)", persisted=True), nullable=True),
    sa.CheckConstraint("scope in ('user','project','session')", name=op.f('ck_item_scope_valid')),
    sa.CheckConstraint("vault <> 'default' OR project_id IS NULL", name=op.f('ck_item_default_no_project')),
    sa.CheckConstraint("vault <> 'ephemeral' OR expires_at IS NOT NULL", name=op.f('ck_item_ephemeral_expires')),
    sa.CheckConstraint("vault <> 'project' OR project_id IS NOT NULL", name=op.f('ck_item_project_needs_id')),
    sa.CheckConstraint("vault in ('default', 'project', 'ephemeral')", name=op.f('ck_item_vault_valid')),
    sa.CheckConstraint('importance >= 0 AND importance <= 1', name=op.f('ck_item_importance_range')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_item')),
    schema='memory'
    )
    op.create_index('ix_memory_embedding', 'item', ['embedding'], unique=False, schema='memory', postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'})
    op.create_index('ix_memory_expires_at', 'item', ['expires_at'], unique=False, schema='memory', postgresql_where=sa.text('expires_at IS NOT NULL'))
    op.create_index('ix_memory_search_vector', 'item', ['search_vector'], unique=False, schema='memory', postgresql_using='gin')
    op.create_index('ix_memory_vault_project', 'item', ['vault', 'project_id'], unique=False, schema='memory')
    op.create_table('record',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('vault', sa.Text(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=True),
    sa.Column('run_id', sa.UUID(), nullable=True),
    sa.Column('source_kind', sa.Text(), nullable=False),
    sa.Column('tool_id', sa.Text(), nullable=True),
    sa.Column('summary', sa.Text(), server_default='', nullable=False),
    sa.Column('media_type', sa.Text(), server_default='text/plain', nullable=False),
    sa.Column('content_hash', sa.Text(), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('blob_id', sa.UUID(), nullable=True),
    sa.Column('trust', sa.Text(), server_default='RAW', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.CheckConstraint("source_kind in ('manual', 'tool', 'llm', 'code', 'research', 'import')", name=op.f('ck_record_source_kind_valid')),
    sa.CheckConstraint("trust = 'RAW'", name=op.f('ck_record_raw_only')),
    sa.CheckConstraint("vault <> 'default' OR project_id IS NULL", name=op.f('ck_record_default_no_project')),
    sa.CheckConstraint("vault <> 'ephemeral' OR expires_at IS NOT NULL", name=op.f('ck_record_ephemeral_expires')),
    sa.CheckConstraint("vault <> 'project' OR project_id IS NOT NULL", name=op.f('ck_record_project_needs_id')),
    sa.CheckConstraint("vault in ('default', 'project', 'ephemeral')", name=op.f('ck_record_vault_valid')),
    sa.ForeignKeyConstraint(['blob_id'], ['evidence.blob.id'], name=op.f('fk_record_blob_id_blob'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_record')),
    schema='evidence'
    )
    op.create_index('ix_record_content_hash', 'record', ['content_hash'], unique=False, schema='evidence')
    op.create_index('ix_record_run', 'record', ['run_id'], unique=False, schema='evidence')
    op.create_index('ix_record_vault_project', 'record', ['vault', 'project_id'], unique=False, schema='evidence')
    op.create_table('observation',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('evidence_id', sa.UUID(), nullable=False),
    sa.Column('statement', sa.Text(), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('created_by', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('confidence >= 0 AND confidence <= 1', name=op.f('ck_observation_confidence_range')),
    sa.ForeignKeyConstraint(['evidence_id'], ['evidence.record.id'], name=op.f('fk_observation_evidence_id_record'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_observation')),
    schema='evidence'
    )
    op.create_index('ix_observation_evidence', 'observation', ['evidence_id'], unique=False, schema='evidence')
    op.create_table('provenance',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('item_id', sa.UUID(), nullable=False),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('source_version', sa.Text(), nullable=True),
    sa.Column('actor', sa.Text(), nullable=False),
    sa.Column('evidence_id', sa.UUID(), nullable=True),
    sa.Column('evidence_hash', sa.Text(), nullable=True),
    sa.Column('notes', sa.Text(), server_default='', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['evidence_id'], ['evidence.record.id'], name=op.f('fk_provenance_evidence_id_record'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['item_id'], ['knowledge.item.id'], name=op.f('fk_provenance_item_id_item'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_provenance')),
    schema='knowledge'
    )
    op.create_index('ix_provenance_evidence', 'provenance', ['evidence_id'], unique=False, schema='knowledge')
    op.create_index('ix_provenance_item', 'provenance', ['item_id'], unique=False, schema='knowledge')


def downgrade() -> None:
    op.drop_index('ix_provenance_item', table_name='provenance', schema='knowledge')
    op.drop_index('ix_provenance_evidence', table_name='provenance', schema='knowledge')
    op.drop_table('provenance', schema='knowledge')
    op.drop_index('ix_observation_evidence', table_name='observation', schema='evidence')
    op.drop_table('observation', schema='evidence')
    op.drop_index('ix_record_vault_project', table_name='record', schema='evidence')
    op.drop_index('ix_record_run', table_name='record', schema='evidence')
    op.drop_index('ix_record_content_hash', table_name='record', schema='evidence')
    op.drop_table('record', schema='evidence')
    op.drop_index('ix_memory_vault_project', table_name='item', schema='memory')
    op.drop_index('ix_memory_search_vector', table_name='item', schema='memory', postgresql_using='gin')
    op.drop_index('ix_memory_expires_at', table_name='item', schema='memory', postgresql_where=sa.text('expires_at IS NOT NULL'))
    op.drop_index('ix_memory_embedding', table_name='item', schema='memory', postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'})
    op.drop_table('item', schema='memory')
    op.drop_index('ix_item_vault_project', table_name='item', schema='knowledge')
    op.drop_index('ix_item_subject_key', table_name='item', schema='knowledge')
    op.drop_index('ix_item_search_vector', table_name='item', schema='knowledge', postgresql_using='gin')
    op.drop_index('ix_item_health', table_name='item', schema='knowledge')
    op.drop_index('ix_item_expires_at', table_name='item', schema='knowledge', postgresql_where=sa.text('expires_at IS NOT NULL'))
    op.drop_index('ix_item_embedding', table_name='item', schema='knowledge', postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'})
    op.drop_table('item', schema='knowledge')
    op.drop_index('ix_decision_vault_project', table_name='decision', schema='knowledge')
    op.drop_index('ix_decision_search_vector', table_name='decision', schema='knowledge', postgresql_using='gin')
    op.drop_table('decision', schema='knowledge')
    op.drop_table('finding', schema='evidence')
    op.drop_table('blob', schema='evidence')
    for schema in ("memory", "knowledge", "evidence"):
        op.execute(f"DROP SCHEMA IF EXISTS {schema}")
    op.execute("DROP EXTENSION IF EXISTS vector")
