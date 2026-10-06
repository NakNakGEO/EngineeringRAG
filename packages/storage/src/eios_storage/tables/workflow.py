"""Schema ``workflow``: workflow runs and node runs (Phase 7)."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from eios_storage.metadata import metadata
from eios_storage.tables._common import in_list

SCHEMA = "workflow"
WORKFLOW_STATUSES = ("running", "waiting_approval", "completed", "failed", "cancelled")
NODE_STATUSES = ("active", "waiting_approval", "passed", "failed", "skipped")

run = sa.Table(
    "run",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("run_id", UUID(as_uuid=True), nullable=False),
    sa.Column("definition_id", sa.Text, nullable=False),
    sa.Column("definition_version", sa.Text, nullable=False),
    sa.Column("definition", JSONB, nullable=False),
    sa.Column("goal", sa.Text, nullable=False),
    sa.Column("project_id", UUID(as_uuid=True), nullable=True),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("current_node", sa.Text, nullable=True),
    sa.Column("risk", sa.Text, nullable=False),
    sa.Column("team", JSONB, nullable=False),
    sa.Column("steps", sa.Integer, nullable=False, server_default="0"),
    sa.Column("version", sa.Integer, nullable=False, server_default="0"),
    sa.Column("error", sa.Text, nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint(in_list("status", WORKFLOW_STATUSES), name="status_valid"),
    sa.CheckConstraint("risk in ('low','medium','high','critical')", name="risk_valid"),
    sa.CheckConstraint(
        "(status IN ('completed','failed','cancelled')) = (finished_at IS NOT NULL)",
        name="finished_matches_status",
    ),
    sa.Index("ix_workflow_run_status", "status", "created_at"),
    sa.Index("ix_workflow_run_run", "run_id"),
    schema=SCHEMA,
)

node_run = sa.Table(
    "node_run",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("workflow_id", UUID(as_uuid=True), nullable=False),
    sa.Column("node_id", sa.Text, nullable=False),
    sa.Column("stage", sa.Text, nullable=False),
    sa.Column("attempt", sa.Integer, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("reporter", sa.Text, nullable=True),
    sa.Column("outputs", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("criteria", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("error", sa.Text, nullable=True),
    sa.Column("approval_id", UUID(as_uuid=True), nullable=True),
    sa.ForeignKeyConstraint(
        ["workflow_id"], ["workflow.run.id"], ondelete="CASCADE", name="fk_node_run_workflow"
    ),
    sa.CheckConstraint(in_list("status", NODE_STATUSES), name="status_valid"),
    sa.CheckConstraint("attempt >= 1", name="attempt_positive"),
    sa.UniqueConstraint("workflow_id", "node_id", "attempt", name="uq_node_attempt"),
    sa.Index("ix_node_run_workflow", "workflow_id", "started_at"),
    schema=SCHEMA,
)
