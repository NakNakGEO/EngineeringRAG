"""Schema ``workshop``: proposed skills, agents and tools and their lifecycle (Phase 10)."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from eios_storage.metadata import metadata
from eios_storage.tables._common import in_list

SCHEMA = "workshop"
PROPOSAL_KINDS = ("skill", "agent", "tool")
PROPOSAL_STATES = (
    "DRAFT", "SANDBOXED", "TESTED", "EXPERIMENTAL", "VERIFIED", "TRUSTED", "REJECTED",
)  # fmt: skip

proposal = sa.Table(
    "proposal",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("version", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("spec", JSONB, nullable=False),
    sa.Column("spec_hash", sa.Text, nullable=False),
    sa.Column("source", sa.Text, nullable=True),
    sa.Column("source_sha256", sa.Text, nullable=True),
    sa.Column("creator", sa.Text, nullable=False),
    sa.Column("creator_kind", sa.Text, nullable=False),
    sa.Column("prompt_hash", sa.Text, nullable=True),
    sa.Column("need", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("capability_claims", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("tests", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("test_results", JSONB, nullable=True),
    sa.Column("scan_report", JSONB, nullable=True),
    sa.Column("approval", JSONB, nullable=True),
    sa.Column("registered_ref", sa.Text, nullable=True),
    sa.Column("artifact_path", sa.Text, nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(in_list("kind", PROPOSAL_KINDS), name="kind_valid"),
    sa.CheckConstraint(in_list("state", PROPOSAL_STATES), name="state_valid"),
    sa.CheckConstraint("creator_kind in ('llm','human','system')", name="creator_kind_valid"),
    sa.CheckConstraint("name !~ '^external_database'", name="not_external_database"),
    sa.Index("ix_proposal_state", "state", "created_at"),
    sa.Index("ix_proposal_name", "kind", "name", "version"),
    schema=SCHEMA,
)

history = sa.Table(
    "history",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("proposal_id", UUID(as_uuid=True), nullable=False),
    sa.Column("from_state", sa.Text, nullable=True),
    sa.Column("to_state", sa.Text, nullable=False),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("reason", sa.Text, nullable=False, server_default=""),
    sa.Column("detail", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(
        ["proposal_id"], ["workshop.proposal.id"], ondelete="CASCADE", name="fk_history_proposal"
    ),
    sa.Index("ix_workshop_history_proposal", "proposal_id", "at"),
    schema=SCHEMA,
)
