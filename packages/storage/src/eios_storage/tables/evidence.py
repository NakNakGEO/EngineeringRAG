"""Schema ``evidence``: raw evidence, observations and findings (the evidence pipeline)."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from eios_storage.metadata import metadata
from eios_storage.tables._common import SOURCE_KINDS, in_list, vault_checks

SCHEMA = "evidence"

blob = sa.Table(
    "blob",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("sha256", sa.Text, nullable=False),
    sa.Column("size_bytes", sa.BigInteger, nullable=False),
    sa.Column("storage_path", sa.Text, nullable=False),
    sa.Column("media_type", sa.Text, nullable=False, server_default="application/octet-stream"),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("sha256", name="uq_blob_sha256"),
    sa.CheckConstraint("size_bytes >= 0", name="size_nonnegative"),
    schema=SCHEMA,
)

record = sa.Table(
    "record",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("vault", sa.Text, nullable=False),
    sa.Column("project_id", UUID(as_uuid=True), nullable=True),
    sa.Column("run_id", UUID(as_uuid=True), nullable=True),
    sa.Column("source_kind", sa.Text, nullable=False),
    sa.Column("tool_id", sa.Text, nullable=True),
    sa.Column("summary", sa.Text, nullable=False, server_default=""),
    sa.Column("media_type", sa.Text, nullable=False, server_default="text/plain"),
    sa.Column("content_hash", sa.Text, nullable=False),
    sa.Column("size_bytes", sa.BigInteger, nullable=False),
    sa.Column(
        "blob_id",
        UUID(as_uuid=True),
        sa.ForeignKey("evidence.blob.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    sa.Column("trust", sa.Text, nullable=False, server_default="RAW"),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    *vault_checks(),
    sa.CheckConstraint(in_list("source_kind", SOURCE_KINDS), name="source_kind_valid"),
    sa.CheckConstraint("trust = 'RAW'", name="raw_only"),  # raw evidence is never more than RAW
    sa.Index("ix_record_vault_project", "vault", "project_id"),
    sa.Index("ix_record_run", "run_id"),
    sa.Index("ix_record_content_hash", "content_hash"),
    schema=SCHEMA,
)

observation = sa.Table(
    "observation",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "evidence_id",
        UUID(as_uuid=True),
        sa.ForeignKey("evidence.record.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("statement", sa.Text, nullable=False),
    sa.Column("confidence", sa.Float, nullable=False),
    sa.Column("created_by", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    sa.Index("ix_observation_evidence", "evidence_id"),
    schema=SCHEMA,
)

finding = sa.Table(
    "finding",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("observation_ids", ARRAY(UUID(as_uuid=True)), nullable=False),
    sa.Column("statement", sa.Text, nullable=False),
    sa.Column("confidence", sa.Float, nullable=False),
    sa.Column("limitations", sa.Text, nullable=False, server_default=""),
    sa.Column("created_by", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    sa.CheckConstraint("cardinality(observation_ids) >= 1", name="needs_observation"),
    schema=SCHEMA,
)
