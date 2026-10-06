"""Schema ``graph``: the code graph. Edges reference nodes by *key*, not by id, so a file being
added, changed or removed never leaves a dangling reference; an unresolved key simply has no node
row yet (e.g. an external package)."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from eios_storage.metadata import metadata

SCHEMA = "graph"

node = sa.Table(
    "node",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("project_id", UUID(as_uuid=True), nullable=False),
    sa.Column("branch", sa.Text, nullable=False),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("key", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("label", sa.Text, nullable=False),
    sa.Column("path", sa.Text, nullable=True),  # file that owns the node, if any
    sa.Column("attrs", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.UniqueConstraint("project_id", "branch", "scope", "key", name="uq_node_key"),
    sa.CheckConstraint("scope in ('committed','overlay')", name="scope_valid"),
    sa.Index("ix_node_kind", "project_id", "branch", "scope", "kind"),
    sa.Index("ix_node_path", "project_id", "branch", "scope", "path"),
    schema=SCHEMA,
)

edge = sa.Table(
    "edge",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("project_id", UUID(as_uuid=True), nullable=False),
    sa.Column("branch", sa.Text, nullable=False),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("src_key", sa.Text, nullable=False),
    sa.Column("dst_key", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("owner_path", sa.Text, nullable=True),  # file that declares the edge
    sa.Column("weight", sa.Float, nullable=False, server_default="1"),
    sa.Column("confidence", sa.Float, nullable=False, server_default="1"),
    sa.Column("attrs", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.UniqueConstraint(
        "project_id", "branch", "scope", "src_key", "dst_key", "kind", name="uq_edge"
    ),
    sa.CheckConstraint("scope in ('committed','overlay')", name="scope_valid"),
    sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    sa.Index("ix_edge_src", "project_id", "branch", "scope", "src_key"),
    sa.Index("ix_edge_dst", "project_id", "branch", "scope", "dst_key"),
    sa.Index("ix_edge_owner", "project_id", "branch", "scope", "owner_path"),
    schema=SCHEMA,
)
