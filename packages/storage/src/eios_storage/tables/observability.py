"""Schema ``observability``: the append-only event log."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from eios_storage.metadata import metadata

SCHEMA = "observability"

event = sa.Table(
    "event",
    metadata,
    sa.Column("event_id", UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "run_id",
        UUID(as_uuid=True),
        sa.ForeignKey("platform.run.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("seq", sa.Integer, nullable=False),
    sa.Column("trace_id", UUID(as_uuid=True), nullable=False),
    sa.Column("span_id", UUID(as_uuid=True), nullable=False),
    sa.Column("parent_span_id", UUID(as_uuid=True), nullable=True),
    sa.Column("type", sa.Text, nullable=False),
    sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
    sa.Column("actor_type", sa.Text, nullable=False),
    sa.Column("actor_id", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("summary", sa.Text, nullable=False, server_default=""),
    sa.Column("data", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("schema_version", sa.SmallInteger, nullable=False, server_default="1"),
    sa.UniqueConstraint("run_id", "seq", name="uq_event_run_seq"),
    sa.CheckConstraint("seq >= 1", name="seq_positive"),
    sa.CheckConstraint("status in ('started','completed','failed','denied')", name="status_valid"),
    sa.Index("ix_event_run_type", "run_id", "type"),
    sa.Index("ix_event_run_span", "run_id", "span_id"),
    sa.Index("ix_event_type_timestamp", "type", "timestamp"),
    schema=SCHEMA,
)
