"""Schema ``source``: indexed files, symbols and searchable text chunks (per branch and scope)."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID

from eios_storage.metadata import metadata

SCHEMA = "source"
SCOPES = ("committed", "overlay")

file = sa.Table(
    "file",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "project_id",
        UUID(as_uuid=True),
        sa.ForeignKey("project.project.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("branch", sa.Text, nullable=False),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("path", sa.Text, nullable=False),
    sa.Column("language", sa.Text, nullable=False),
    sa.Column("size_bytes", sa.BigInteger, nullable=False),
    sa.Column("line_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("content_hash", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="active"),
    sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.UniqueConstraint("project_id", "branch", "scope", "path", name="uq_file_location"),
    sa.CheckConstraint("scope in ('committed','overlay')", name="scope_valid"),
    sa.CheckConstraint("status in ('active','deleted')", name="status_valid"),
    # overlay (uncommitted) state is temporary by definition; committed state never expires
    sa.CheckConstraint(
        "(scope = 'overlay' AND expires_at IS NOT NULL)"
        " OR (scope = 'committed' AND expires_at IS NULL)",
        name="overlay_expires",
    ),
    sa.Index("ix_file_project_branch_scope", "project_id", "branch", "scope"),
    sa.Index(
        "ix_file_expires_at", "expires_at", postgresql_where=sa.text("expires_at IS NOT NULL")
    ),
    schema=SCHEMA,
)

symbol = sa.Table(
    "symbol",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "file_id",
        UUID(as_uuid=True),
        sa.ForeignKey("source.file.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("project_id", UUID(as_uuid=True), nullable=False),
    sa.Column("branch", sa.Text, nullable=False),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("name_lower", sa.Text, sa.Computed("lower(name)", persisted=True)),
    sa.Column("qualified_name", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("container", sa.Text, nullable=True),
    sa.Column("start_line", sa.Integer, nullable=False),
    sa.Column("end_line", sa.Integer, nullable=False),
    sa.Column("signature", sa.Text, nullable=False, server_default=""),
    sa.Column("exported", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Index("ix_symbol_name", "project_id", "branch", "scope", "name_lower"),
    sa.Index("ix_symbol_file", "file_id"),
    schema=SCHEMA,
)


def _words(column: str) -> str:
    """``a_b.c/d-e`` -> ``a b c d e``: punctuation inside identifiers and paths separates words."""
    return f"translate({column}, './_-:', '     ')"


# Heading (A) and path (C) outrank body text (D); camelCase is split so ``RetentionService``
# meets "retention". Whole identifiers are matched through ``idents`` instead.
CHUNK_SEARCH_VECTOR = (
    f"setweight(to_tsvector('english', {_words('heading')}), 'A')"
    f" || setweight(to_tsvector('english', {_words('path')}), 'C')"
    f" || to_tsvector('english', regexp_replace({_words('text')},"
    r" '([a-z0-9])([A-Z])', '\1 \2', 'g'))"
)

# Searchable file text (documents, configuration, SQL, migrations), cut into bounded chunks that
# remember their line range. Rows belong to a file row: they go with it (and with the overlay).
chunk = sa.Table(
    "project_chunk",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "file_id",
        UUID(as_uuid=True),
        sa.ForeignKey("source.file.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("project_id", UUID(as_uuid=True), nullable=False),
    sa.Column("branch", sa.Text, nullable=False),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("path", sa.Text, nullable=False),
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.Column("start_line", sa.Integer, nullable=False),
    sa.Column("end_line", sa.Integer, nullable=False),
    sa.Column("heading", sa.Text, nullable=False, server_default=""),
    sa.Column("text", sa.Text, nullable=False),
    # whole words and dotted names (case preserved): the exact-name lookup of entity resolution
    sa.Column("idents", ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'::text[]")),
    sa.Column("search_vector", TSVECTOR, sa.Computed(CHUNK_SEARCH_VECTOR, persisted=True)),
    sa.UniqueConstraint("file_id", "ordinal", name="uq_project_chunk_file_ordinal"),
    sa.CheckConstraint("scope in ('committed','overlay')", name="scope_valid"),
    sa.CheckConstraint("start_line >= 1 AND end_line >= start_line", name="lines_valid"),
    sa.Index("ix_project_chunk_location", "project_id", "branch", "scope", "path"),
    sa.Index("ix_project_chunk_search_vector", "search_vector", postgresql_using="gin"),
    sa.Index("ix_project_chunk_idents", "idents", postgresql_using="gin"),
    schema=SCHEMA,
)
