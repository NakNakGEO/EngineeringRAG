"""Schema ``project``: project identity, locations and per-sync snapshots."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from eios_storage.metadata import metadata
from eios_storage.tables._common import in_list

SCHEMA = "project"
BOOTSTRAP_STATES = (
    "NEW", "CURRENT", "STALE", "DIRTY", "BRANCH_CHANGED", "MAJOR_DIVERGENCE", "ERROR",
)  # fmt: skip

project = sa.Table(
    "project",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("fingerprint", sa.Text, nullable=False),
    sa.Column("remote", sa.Text, nullable=True),  # normalised remote, never with credentials
    sa.Column("root_commit", sa.Text, nullable=True),
    sa.Column("local_root", sa.Text, nullable=False),
    sa.Column("default_branch", sa.Text, nullable=True),
    sa.Column("bootstrap_state", sa.Text, nullable=False),
    sa.Column("last_branch", sa.Text, nullable=True),
    sa.Column("last_commit", sa.Text, nullable=True),
    sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.UniqueConstraint("fingerprint", name="uq_project_fingerprint"),
    sa.CheckConstraint(in_list("bootstrap_state", BOOTSTRAP_STATES), name="state_valid"),
    schema=SCHEMA,
)

location = sa.Table(
    "location",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "project_id",
        UUID(as_uuid=True),
        sa.ForeignKey("project.project.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("path", sa.Text, nullable=False),
    sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("project_id", "path", name="uq_location_project_path"),
    schema=SCHEMA,
)

snapshot = sa.Table(
    "snapshot",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "project_id",
        UUID(as_uuid=True),
        sa.ForeignKey("project.project.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("branch", sa.Text, nullable=False),
    sa.Column("commit_sha", sa.Text, nullable=False),
    sa.Column("dirty", sa.Boolean, nullable=False),
    sa.Column("dirty_paths", sa.Integer, nullable=False, server_default="0"),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("files_total", sa.Integer, nullable=False, server_default="0"),
    sa.Column("files_changed", sa.Integer, nullable=False, server_default="0"),
    sa.Column("symbols_total", sa.Integer, nullable=False, server_default="0"),
    sa.Column("duration_ms", sa.Integer, nullable=False, server_default="0"),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(in_list("state", BOOTSTRAP_STATES), name="state_valid"),
    sa.Index("ix_snapshot_project_created", "project_id", "created_at"),
    schema=SCHEMA,
)
