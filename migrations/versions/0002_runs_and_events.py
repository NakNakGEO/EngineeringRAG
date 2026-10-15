"""Runs and the append-only event log (Phase 1).

Creates schemas ``platform`` and ``observability``, ``platform.run`` and
``observability.event``. The event table is append-only: a trigger rejects UPDATE and DELETE.
DELETE is only permitted inside the retention job (Phase 12), which sets the transaction-local
setting ``eios.retention = 'on'``.

Downgrade drops these tables and ALL recorded events.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06 01:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS platform")
    op.execute("CREATE SCHEMA IF NOT EXISTS observability")

    op.create_table(
        "run",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_run"),
        sa.CheckConstraint(
            "status in ('pending','running','completed','failed','cancelled')",
            name="ck_run_status_valid",
        ),
        schema="platform",
    )
    op.create_index("ix_run_created_at", "run", ["created_at"], schema="platform")
    op.create_index("ix_run_status", "run", ["status"], schema="platform")

    op.create_table(
        "event",
        sa.Column("event_id", UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", UUID(as_uuid=True), nullable=False),
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
        sa.PrimaryKeyConstraint("event_id", name="pk_event"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["platform.run.id"],
            name="fk_event_run_id_run",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("run_id", "seq", name="uq_event_run_seq"),
        sa.CheckConstraint("seq >= 1", name="ck_event_seq_positive"),
        sa.CheckConstraint(
            "status in ('started','completed','failed','denied')", name="ck_event_status_valid"
        ),
        schema="observability",
    )
    op.create_index("ix_event_run_type", "event", ["run_id", "type"], schema="observability")
    op.create_index("ix_event_run_span", "event", ["run_id", "span_id"], schema="observability")
    op.create_index(
        "ix_event_type_timestamp", "event", ["type", "timestamp"], schema="observability"
    )

    op.execute(
        """
        CREATE FUNCTION observability.forbid_event_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND current_setting('eios.retention', true) = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'observability.event is append-only (% rejected)', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER event_append_only
        BEFORE UPDATE OR DELETE ON observability.event
        FOR EACH ROW EXECUTE FUNCTION observability.forbid_event_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS event_append_only ON observability.event")
    op.execute("DROP FUNCTION IF EXISTS observability.forbid_event_mutation()")
    op.drop_table("event", schema="observability")
    op.drop_table("run", schema="platform")
    op.execute("DROP SCHEMA IF EXISTS observability")
    op.execute("DROP SCHEMA IF EXISTS platform")
