"""Schema ``policy``: human approvals and the append-only audit log (Phase 6)."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from eios_storage.metadata import metadata
from eios_storage.tables._common import in_list

SCHEMA = "policy"
APPROVAL_STATUSES = ("pending", "approved", "denied", "expired", "consumed")

approval = sa.Table(
    "approval",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("fingerprint", sa.Text, nullable=False),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("capability", sa.Text, nullable=True),
    sa.Column("target", sa.Text, nullable=True),
    sa.Column("requested_by", sa.Text, nullable=False),
    sa.Column("run_id", UUID(as_uuid=True), nullable=True),
    sa.Column("request", JSONB, nullable=False),
    sa.Column("reason", sa.Text, nullable=False, server_default=""),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("decided_by", sa.Text, nullable=True),
    sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("decision_note", sa.Text, nullable=False, server_default=""),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint(in_list("status", APPROVAL_STATUSES), name="status_valid"),
    sa.CheckConstraint(
        "(status IN ('approved','denied','consumed')) = (decided_by IS NOT NULL)",
        name="decision_has_decider",
    ),
    sa.Index("ix_approval_fingerprint", "fingerprint", "status"),
    sa.Index("ix_approval_status_created", "status", "created_at"),
    schema=SCHEMA,
)

audit_log = sa.Table(
    "audit_log",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("actor_type", sa.Text, nullable=False),
    sa.Column("actor_id", sa.Text, nullable=False),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("capability", sa.Text, nullable=True),
    sa.Column("target", sa.Text, nullable=True),
    sa.Column("effect", sa.Text, nullable=False),
    sa.Column("rule_id", sa.Text, nullable=False),
    sa.Column("risk", sa.Text, nullable=False),
    sa.Column("reasons", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("run_id", UUID(as_uuid=True), nullable=True),
    sa.Column("root_policy_version", sa.Text, nullable=True),
    sa.Column("detail", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.CheckConstraint("effect in ('allow','deny','require_approval')", name="effect_valid"),
    sa.Index("ix_audit_at", "at"),
    sa.Index("ix_audit_effect_at", "effect", "at"),
    sa.Index("ix_audit_run", "run_id"),
    schema=SCHEMA,
)
