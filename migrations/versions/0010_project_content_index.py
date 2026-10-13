"""Searchable file text: documents, configuration, SQL and migrations in chunks (ADR 0018).

Adds ``source.project_chunk``. Chunks hang off ``source.file`` (deleting a file or the overlay
deletes them) and carry a full-text vector plus the identifiers they mention. Existing projects
have no chunks until their files are next indexed: the sync detects that and re-reads them.

Downgrade drops ``source.project_chunk`` and ALL indexed file text (the file, symbol and graph
rows are kept; the next sync after upgrading again rebuilds the text).

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-13 09:41:12.507164+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0010'
down_revision: str | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('project_chunk',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('file_id', sa.UUID(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=False),
    sa.Column('branch', sa.Text(), nullable=False),
    sa.Column('scope', sa.Text(), nullable=False),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('ordinal', sa.Integer(), nullable=False),
    sa.Column('start_line', sa.Integer(), nullable=False),
    sa.Column('end_line', sa.Integer(), nullable=False),
    sa.Column('heading', sa.Text(), server_default='', nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('idents', postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False),
    sa.Column('search_vector', postgresql.TSVECTOR(), sa.Computed(r"setweight(to_tsvector('english', translate(heading, './_-:', '     ')), 'A') || setweight(to_tsvector('english', translate(path, './_-:', '     ')), 'C') || to_tsvector('english', regexp_replace(translate(text, './_-:', '     '), '([a-z0-9])([A-Z])', '\1 \2', 'g'))", persisted=True), nullable=True),
    sa.CheckConstraint('start_line >= 1 AND end_line >= start_line', name=op.f('ck_project_chunk_lines_valid')),
    sa.CheckConstraint("scope in ('committed','overlay')", name=op.f('ck_project_chunk_scope_valid')),
    sa.ForeignKeyConstraint(['file_id'], ['source.file.id'], name=op.f('fk_project_chunk_file_id_file'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_project_chunk')),
    sa.UniqueConstraint('file_id', 'ordinal', name='uq_project_chunk_file_ordinal'),
    schema='source'
    )
    op.create_index('ix_project_chunk_idents', 'project_chunk', ['idents'], unique=False, schema='source', postgresql_using='gin')
    op.create_index('ix_project_chunk_location', 'project_chunk', ['project_id', 'branch', 'scope', 'path'], unique=False, schema='source')
    op.create_index('ix_project_chunk_search_vector', 'project_chunk', ['search_vector'], unique=False, schema='source', postgresql_using='gin')


def downgrade() -> None:
    op.drop_index('ix_project_chunk_search_vector', table_name='project_chunk', schema='source', postgresql_using='gin')
    op.drop_index('ix_project_chunk_location', table_name='project_chunk', schema='source')
    op.drop_index('ix_project_chunk_idents', table_name='project_chunk', schema='source', postgresql_using='gin')
    op.drop_table('project_chunk', schema='source')
