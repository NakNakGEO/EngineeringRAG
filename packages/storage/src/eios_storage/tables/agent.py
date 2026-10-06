"""Schema ``agent``: agent (reasoning role) definitions."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from eios_storage.metadata import metadata
from eios_storage.tables._common import in_list
from eios_storage.tables.capability import ORIGINS, REGISTRY_STATES

SCHEMA = "agent"

definition = sa.Table(
    "definition",
    metadata,
    sa.Column("id", sa.Text, nullable=False),
    sa.Column("version", sa.Text, nullable=False),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("origin", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("can_write", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("manifest", JSONB, nullable=False),
    sa.Column("manifest_hash", sa.Text, nullable=False),
    sa.Column("approved_by", sa.Text, nullable=True),
    sa.Column("approval_id", UUID(as_uuid=True), nullable=True),
    sa.Column("state_reason", sa.Text, nullable=False, server_default=""),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint("id", "version", name="pk_definition"),
    sa.CheckConstraint(in_list("state", REGISTRY_STATES), name="state_valid"),
    sa.CheckConstraint(in_list("origin", ORIGINS), name="origin_valid"),
    sa.Index("ix_agent_role", "role"),
    schema=SCHEMA,
)
