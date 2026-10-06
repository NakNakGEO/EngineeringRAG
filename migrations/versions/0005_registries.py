"""Capability catalog, providers, metrics, agents and skills (Phase 5).

Creates schemas ``capability``, ``agent``, ``skill`` and their tables.

Downgrade drops them and ALL registered capabilities, providers, agents, skills and metrics.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-06 12:00:59.846033+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for schema in ("capability", "agent", "skill"):
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

    op.create_table('definition',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('version', sa.Text(), nullable=False),
    sa.Column('role', sa.Text(), nullable=False),
    sa.Column('origin', sa.Text(), nullable=False),
    sa.Column('state', sa.Text(), nullable=False),
    sa.Column('can_write', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('manifest', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('manifest_hash', sa.Text(), nullable=False),
    sa.Column('approved_by', sa.Text(), nullable=True),
    sa.Column('approval_id', sa.UUID(), nullable=True),
    sa.Column('state_reason', sa.Text(), server_default='', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("origin in ('builtin', 'plugin', 'generated', 'downloaded')", name=op.f('ck_definition_origin_valid')),
    sa.CheckConstraint("state in ('UNREGISTERED', 'EXPERIMENTAL', 'VERIFIED', 'TRUSTED', 'DISABLED', 'BROKEN', 'QUARANTINED')", name=op.f('ck_definition_state_valid')),
    sa.PrimaryKeyConstraint('id', 'version', name='pk_definition'),
    schema='agent'
    )
    op.create_index('ix_agent_role', 'definition', ['role'], unique=False, schema='agent')
    op.create_table('definition',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('risk', sa.Text(), nullable=False),
    sa.Column('inputs', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('outputs', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('allowed_scopes', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('tags', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('manifest', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('manifest_hash', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("id !~ '^external_database'", name=op.f('ck_definition_not_external_database')),
    sa.CheckConstraint("risk in ('low','medium','high','critical')", name=op.f('ck_definition_risk_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_definition')),
    schema='capability'
    )
    op.create_table('provider',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('version', sa.Text(), nullable=False),
    sa.Column('type', sa.Text(), nullable=False),
    sa.Column('origin', sa.Text(), nullable=False),
    sa.Column('state', sa.Text(), nullable=False),
    sa.Column('maturity', sa.Text(), nullable=False),
    sa.Column('trust', sa.Text(), server_default='unrated', nullable=False),
    sa.Column('verification_state', sa.Text(), server_default='unverified', nullable=False),
    sa.Column('priority', sa.Integer(), server_default='50', nullable=False),
    sa.Column('manifest', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('manifest_hash', sa.Text(), nullable=False),
    sa.Column('approved_by', sa.Text(), nullable=True),
    sa.Column('approval_id', sa.UUID(), nullable=True),
    sa.Column('health_status', sa.Text(), server_default='unknown', nullable=False),
    sa.Column('health_checked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('health_detail', sa.Text(), server_default='', nullable=False),
    sa.Column('state_reason', sa.Text(), server_default='', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("health_status in ('unknown','ok','fail')", name=op.f('ck_provider_health_valid')),
    sa.CheckConstraint("origin in ('builtin', 'plugin', 'generated', 'downloaded')", name=op.f('ck_provider_origin_valid')),
    sa.CheckConstraint("state in ('UNREGISTERED', 'EXPERIMENTAL', 'VERIFIED', 'TRUSTED', 'DISABLED', 'BROKEN', 'QUARANTINED')", name=op.f('ck_provider_state_valid')),
    sa.PrimaryKeyConstraint('id', 'version', name='pk_provider'),
    schema='capability'
    )
    op.create_index('ix_provider_state', 'provider', ['state'], unique=False, schema='capability')
    op.create_table('state_history',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('item_id', sa.Text(), nullable=False),
    sa.Column('item_version', sa.Text(), nullable=False),
    sa.Column('from_state', sa.Text(), nullable=False),
    sa.Column('to_state', sa.Text(), nullable=False),
    sa.Column('actor', sa.Text(), nullable=False),
    sa.Column('reason', sa.Text(), server_default='', nullable=False),
    sa.Column('approval_id', sa.UUID(), nullable=True),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("kind in ('provider','agent','skill')", name=op.f('ck_state_history_kind_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_state_history')),
    schema='capability'
    )
    op.create_index('ix_state_history_item', 'state_history', ['kind', 'item_id', 'item_version'], unique=False, schema='capability')
    op.create_table('definition',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('version', sa.Text(), nullable=False),
    sa.Column('origin', sa.Text(), nullable=False),
    sa.Column('state', sa.Text(), nullable=False),
    sa.Column('manifest', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('manifest_hash', sa.Text(), nullable=False),
    sa.Column('approved_by', sa.Text(), nullable=True),
    sa.Column('approval_id', sa.UUID(), nullable=True),
    sa.Column('state_reason', sa.Text(), server_default='', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("origin in ('builtin', 'plugin', 'generated', 'downloaded')", name=op.f('ck_definition_origin_valid')),
    sa.CheckConstraint("state in ('UNREGISTERED', 'EXPERIMENTAL', 'VERIFIED', 'TRUSTED', 'DISABLED', 'BROKEN', 'QUARANTINED')", name=op.f('ck_definition_state_valid')),
    sa.PrimaryKeyConstraint('id', 'version', name='pk_definition'),
    schema='skill'
    )
    op.create_table('provider_capability',
    sa.Column('provider_id', sa.Text(), nullable=False),
    sa.Column('provider_version', sa.Text(), nullable=False),
    sa.Column('capability_id', sa.Text(), nullable=False),
    sa.CheckConstraint("capability_id !~ '^external_database'", name=op.f('ck_provider_capability_not_external_database')),
    sa.ForeignKeyConstraint(['provider_id', 'provider_version'], ['capability.provider.id', 'capability.provider.version'], name='fk_provider_capability_provider', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('provider_id', 'provider_version', 'capability_id', name='pk_provider_capability'),
    schema='capability'
    )
    op.create_index('ix_provider_capability_capability', 'provider_capability', ['capability_id'], unique=False, schema='capability')
    op.create_table('provider_metric',
    sa.Column('provider_id', sa.Text(), nullable=False),
    sa.Column('provider_version', sa.Text(), nullable=False),
    sa.Column('capability_id', sa.Text(), nullable=False),
    sa.Column('successes', sa.Integer(), server_default='0', nullable=False),
    sa.Column('failures', sa.Integer(), server_default='0', nullable=False),
    sa.Column('total_latency_ms', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('tokens', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('eval_score', sa.Float(), nullable=True),
    sa.Column('eval_samples', sa.Integer(), server_default='0', nullable=False),
    sa.Column('known_weaknesses', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('successes >= 0 AND failures >= 0', name=op.f('ck_provider_metric_counts_nonnegative')),
    sa.ForeignKeyConstraint(['provider_id', 'provider_version'], ['capability.provider.id', 'capability.provider.version'], name='fk_provider_metric_provider', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('provider_id', 'provider_version', 'capability_id', name='pk_provider_metric'),
    schema='capability'
    )


def downgrade() -> None:
    op.drop_table('provider_metric', schema='capability')
    op.drop_index('ix_provider_capability_capability', table_name='provider_capability', schema='capability')
    op.drop_table('provider_capability', schema='capability')
    op.drop_table('definition', schema='skill')
    op.drop_index('ix_state_history_item', table_name='state_history', schema='capability')
    op.drop_table('state_history', schema='capability')
    op.drop_index('ix_provider_state', table_name='provider', schema='capability')
    op.drop_table('provider', schema='capability')
    op.drop_table('definition', schema='capability')
    op.drop_index('ix_agent_role', table_name='definition', schema='agent')
    op.drop_table('definition', schema='agent')
    for schema in ("skill", "agent", "capability"):
        op.execute(f"DROP SCHEMA IF EXISTS {schema}")
