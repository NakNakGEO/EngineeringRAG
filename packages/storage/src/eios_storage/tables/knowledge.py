"""Schema ``knowledge``: knowledge items, provenance and the decision ledger."""

from __future__ import annotations

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID

from eios_storage.metadata import metadata
from eios_storage.tables._common import (
    EMBEDDING_DIM,
    HEALTH_STATES,
    SOURCE_KINDS,
    TRUST_LEVELS,
    in_list,
    vault_checks,
)

SCHEMA = "knowledge"

item = sa.Table(
    "item",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("vault", sa.Text, nullable=False),
    sa.Column("project_id", UUID(as_uuid=True), nullable=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("tags", ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'")),
    sa.Column("trust", sa.Text, nullable=False),
    sa.Column("health", sa.Text, nullable=False),
    sa.Column("confidence", sa.Float, nullable=False),
    sa.Column("source_kind", sa.Text, nullable=False),
    sa.Column("created_by", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("subject_key", sa.Text, nullable=True),
    sa.Column("limitations", sa.Text, nullable=False, server_default=""),
    sa.Column("version_ref", sa.Text, nullable=True),
    sa.Column("superseded_by", UUID(as_uuid=True), nullable=True),
    sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
    sa.Column("embedding_model", sa.Text, nullable=True),
    sa.Column(
        "search_vector",
        TSVECTOR,
        sa.Computed("to_tsvector('english', title || ' ' || content)", persisted=True),
    ),
    *vault_checks(),
    sa.CheckConstraint(in_list("trust", TRUST_LEVELS), name="trust_valid"),
    sa.CheckConstraint(in_list("health", HEALTH_STATES), name="health_valid"),
    sa.CheckConstraint(in_list("source_kind", SOURCE_KINDS), name="source_kind_valid"),
    sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    sa.Index("ix_item_vault_project", "vault", "project_id"),
    sa.Index("ix_item_health", "health"),
    sa.Index("ix_item_subject_key", "subject_key"),
    sa.Index(
        "ix_item_expires_at", "expires_at", postgresql_where=sa.text("expires_at IS NOT NULL")
    ),
    sa.Index("ix_item_search_vector", "search_vector", postgresql_using="gin"),
    sa.Index(
        "ix_item_embedding",
        "embedding",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    ),
    schema=SCHEMA,
)

provenance = sa.Table(
    "provenance",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "item_id",
        UUID(as_uuid=True),
        sa.ForeignKey("knowledge.item.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("source_version", sa.Text, nullable=True),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column(
        "evidence_id",
        UUID(as_uuid=True),
        sa.ForeignKey("evidence.record.id", ondelete="SET NULL"),
        nullable=True,
    ),
    sa.Column("evidence_hash", sa.Text, nullable=True),
    sa.Column("notes", sa.Text, nullable=False, server_default=""),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index("ix_provenance_item", "item_id"),
    sa.Index("ix_provenance_evidence", "evidence_id"),
    schema=SCHEMA,
)

decision = sa.Table(
    "decision",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("vault", sa.Text, nullable=False),
    sa.Column("project_id", UUID(as_uuid=True), nullable=True),
    sa.Column("question", sa.Text, nullable=False),
    sa.Column("alternatives", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("selected", sa.Text, nullable=False, server_default=""),
    sa.Column("rationale", sa.Text, nullable=False, server_default=""),
    sa.Column(
        "evidence_ids", ARRAY(UUID(as_uuid=True)), nullable=False, server_default=sa.text("'{}'")
    ),
    sa.Column("decider", sa.Text, nullable=False),
    sa.Column("reviewers", ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'")),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column(
        "superseded_by",
        UUID(as_uuid=True),
        sa.ForeignKey("knowledge.decision.id", ondelete="SET NULL"),
        nullable=True,
    ),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column(
        "search_vector",
        TSVECTOR,
        sa.Computed(
            "to_tsvector('english', question || ' ' || selected || ' ' || rationale)",
            persisted=True,
        ),
    ),
    *vault_checks(expires=False),
    sa.CheckConstraint("vault <> 'ephemeral'", name="durable_vault"),
    sa.CheckConstraint(
        "status in ('proposed','accepted','rejected','superseded')", name="status_valid"
    ),
    sa.Index("ix_decision_vault_project", "vault", "project_id"),
    sa.Index("ix_decision_search_vector", "search_vector", postgresql_using="gin"),
    schema=SCHEMA,
)
