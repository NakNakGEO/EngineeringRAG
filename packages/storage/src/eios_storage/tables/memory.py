"""Schema ``memory``: durable and session memory items."""

from __future__ import annotations

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR, UUID

from eios_storage.metadata import metadata
from eios_storage.tables._common import EMBEDDING_DIM, vault_checks

SCHEMA = "memory"

item = sa.Table(
    "item",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("vault", sa.Text, nullable=False),
    sa.Column("project_id", UUID(as_uuid=True), nullable=True),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("tags", ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'")),
    sa.Column("importance", sa.Float, nullable=False),
    sa.Column("created_by", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
    sa.Column("embedding_model", sa.Text, nullable=True),
    sa.Column(
        "search_vector", TSVECTOR, sa.Computed("to_tsvector('english', content)", persisted=True)
    ),
    *vault_checks(),
    sa.CheckConstraint("scope in ('user','project','session')", name="scope_valid"),
    sa.CheckConstraint("importance >= 0 AND importance <= 1", name="importance_range"),
    sa.Index("ix_memory_vault_project", "vault", "project_id"),
    sa.Index(
        "ix_memory_expires_at", "expires_at", postgresql_where=sa.text("expires_at IS NOT NULL")
    ),
    sa.Index("ix_memory_search_vector", "search_vector", postgresql_using="gin"),
    sa.Index(
        "ix_memory_embedding",
        "embedding",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    ),
    schema=SCHEMA,
)
