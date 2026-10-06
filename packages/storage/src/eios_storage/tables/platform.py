"""Schema ``platform``: runs and platform-level bookkeeping."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from eios_storage.metadata import metadata

SCHEMA = "platform"

run = sa.Table(
    "run",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("trace_id", UUID(as_uuid=True), nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("goal", sa.Text, nullable=False, server_default=""),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("project_id", UUID(as_uuid=True), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("error", sa.Text, nullable=True),
    sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("budget", JSONB, nullable=True),
    sa.Column("next_seq", sa.Integer, nullable=False, server_default="0"),
    sa.CheckConstraint(
        "status in ('pending','running','completed','failed','cancelled')", name="status_valid"
    ),
    sa.Index("ix_run_created_at", "created_at"),
    sa.Index("ix_run_status", "status"),
    schema=SCHEMA,
)
