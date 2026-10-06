"""Policy approvals and the append-only audit log (Phase 6).

Downgrade drops the policy schema and ALL approvals and audit records.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-06 12:14:43.237173+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS policy")
    op.create_table('approval',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('fingerprint', sa.Text(), nullable=False),
    sa.Column('action', sa.Text(), nullable=False),
    sa.Column('capability', sa.Text(), nullable=True),
    sa.Column('target', sa.Text(), nullable=True),
    sa.Column('requested_by', sa.Text(), nullable=False),
    sa.Column('run_id', sa.UUID(), nullable=True),
    sa.Column('request', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('reason', sa.Text(), server_default='', nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('decided_by', sa.Text(), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decision_note', sa.Text(), server_default='', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("(status IN ('approved','denied','consumed')) = (decided_by IS NOT NULL)", name=op.f('ck_approval_decision_has_decider')),
    sa.CheckConstraint("status in ('pending', 'approved', 'denied', 'expired', 'consumed')", name=op.f('ck_approval_status_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_approval')),
    schema='policy'
    )
    op.create_index('ix_approval_fingerprint', 'approval', ['fingerprint', 'status'], unique=False, schema='policy')
    op.create_index('ix_approval_status_created', 'approval', ['status', 'created_at'], unique=False, schema='policy')
    op.create_table('audit_log',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('actor_type', sa.Text(), nullable=False),
    sa.Column('actor_id', sa.Text(), nullable=False),
    sa.Column('action', sa.Text(), nullable=False),
    sa.Column('capability', sa.Text(), nullable=True),
    sa.Column('target', sa.Text(), nullable=True),
    sa.Column('effect', sa.Text(), nullable=False),
    sa.Column('rule_id', sa.Text(), nullable=False),
    sa.Column('risk', sa.Text(), nullable=False),
    sa.Column('reasons', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('run_id', sa.UUID(), nullable=True),
    sa.Column('root_policy_version', sa.Text(), nullable=True),
    sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.CheckConstraint("effect in ('allow','deny','require_approval')", name=op.f('ck_audit_log_effect_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_log')),
    schema='policy'
    )
    op.create_index('ix_audit_at', 'audit_log', ['at'], unique=False, schema='policy')
    op.create_index('ix_audit_effect_at', 'audit_log', ['effect', 'at'], unique=False, schema='policy')
    op.create_index('ix_audit_run', 'audit_log', ['run_id'], unique=False, schema='policy')
    op.execute(
        """
        CREATE FUNCTION policy.forbid_audit_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND current_setting('eios.retention', true) = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'policy.audit_log is append-only (% rejected)', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_append_only
        BEFORE UPDATE OR DELETE ON policy.audit_log
        FOR EACH ROW EXECUTE FUNCTION policy.forbid_audit_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_append_only ON policy.audit_log")
    op.execute("DROP FUNCTION IF EXISTS policy.forbid_audit_mutation()")
    op.drop_index('ix_audit_run', table_name='audit_log', schema='policy')
    op.drop_index('ix_audit_effect_at', table_name='audit_log', schema='policy')
    op.drop_index('ix_audit_at', table_name='audit_log', schema='policy')
    op.drop_table('audit_log', schema='policy')
    op.drop_index('ix_approval_status_created', table_name='approval', schema='policy')
    op.drop_index('ix_approval_fingerprint', table_name='approval', schema='policy')
    op.drop_table('approval', schema='policy')
    op.execute("DROP SCHEMA IF EXISTS policy")
