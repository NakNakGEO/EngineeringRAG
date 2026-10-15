"""Persistence for projects, indexed files/symbols and the key-based code graph."""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from eios_domain.ids import new_id, utcnow
from eios_domain.project import BootstrapState, FileScope
from eios_project_intelligence.chunking import (
    CHUNK_VERSION,
    CHUNKED_LANGUAGES,
    CODE_TEXT_PATH_SQL,
    ChunkSpec,
)
from eios_storage.tables.graph import edge as edge_t
from eios_storage.tables.graph import node as node_t
from eios_storage.tables.project import file_summary as summary_t
from eios_storage.tables.project import location as location_t
from eios_storage.tables.project import project as project_t
from eios_storage.tables.project import snapshot as snapshot_t
from eios_storage.tables.source import chunk as chunk_t
from eios_storage.tables.source import file as file_t
from eios_storage.tables.source import symbol as symbol_t

_INSERT_CHUNK = 2000
_CHUNK_INSERT = 400  # text chunks are large rows
_CONTENT_GENERIC = 0.5  # a word in more than this share of the text chunks tells nothing apart
_CONTENT_RARE = 3  # a word in at most this many chunks is a name for them: one match is enough


class Project(BaseModel):
    id: uuid.UUID
    name: str
    fingerprint: str
    remote: str | None
    root_commit: str | None
    local_root: str
    default_branch: str | None
    bootstrap_state: BootstrapState
    last_branch: str | None
    last_commit: str | None
    last_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime


def _project(row: Any) -> Project:
    m = dict(row._mapping)
    m.pop("metadata", None)
    return Project(**m)


@dataclass
class GraphNodeSpec:
    key: str
    kind: str
    label: str
    path: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphEdgeSpec:
    src_key: str
    dst_key: str
    kind: str
    owner_path: str | None = None
    confidence: float = 1.0
    weight: float = 1.0
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass
class SymbolSpec:
    name: str
    qualified_name: str
    kind: str
    container: str | None
    start_line: int
    end_line: int
    signature: str
    exported: bool


@dataclass
class FileChange:
    path: str
    language: str
    size_bytes: int
    line_count: int
    content_hash: str
    symbols: list[SymbolSpec] = field(default_factory=list)
    nodes: list[GraphNodeSpec] = field(default_factory=list)
    edges: list[GraphEdgeSpec] = field(default_factory=list)
    chunks: list[ChunkSpec] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = "active"


class FileRow(BaseModel):
    id: uuid.UUID
    path: str
    language: str
    size_bytes: int
    line_count: int
    content_hash: str
    status: str
    scope: str
    branch: str
    indexed_at: datetime
    metadata: dict[str, Any]


class SymbolRow(BaseModel):
    id: uuid.UUID
    file_id: uuid.UUID
    path: str
    scope: str
    name: str
    qualified_name: str
    kind: str
    container: str | None
    start_line: int
    end_line: int
    signature: str
    exported: bool


class ChunkHit(BaseModel):
    """The best-matching chunk of one file (or the first one, for a file picked by its name)."""

    path: str
    scope: str
    language: str
    line_count: int
    start_line: int
    end_line: int
    heading: str
    text: str
    score: float = 0.0
    matches: int = 1  # chunks of this file that matched
    terms_matched: int = 0  # distinct question words in the chunk


def _like_escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _chunks(items: Sequence[Any], size: int = _INSERT_CHUNK) -> Iterable[Sequence[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _wants_chunks() -> sa.ColumnElement[bool]:
    """SQL twin of :func:`eios_project_intelligence.chunking.wants_chunks`."""
    return sa.or_(
        file_t.c.language.in_(sorted(CHUNKED_LANGUAGES)),
        sa.and_(file_t.c.language != "binary", file_t.c.path.op("~")(CODE_TEXT_PATH_SQL)),
    )


def _chunk_visible(scopes: Sequence[FileScope]) -> list[sa.ColumnElement[bool]]:
    """Chunks of the requested scopes. An overlay row for a path (the file was modified, or is a
    deletion tombstone) hides that path's committed chunks, so the working-tree view never mixes
    in text from HEAD."""
    values = [s.value for s in scopes]
    clauses: list[sa.ColumnElement[bool]] = [chunk_t.c.scope.in_(values)]
    if FileScope.OVERLAY.value in values and FileScope.COMMITTED.value in values:
        overlay = file_t.alias("overlay_file")
        clauses.append(
            sa.or_(
                chunk_t.c.scope == FileScope.OVERLAY.value,
                ~sa.exists()
                .where(
                    overlay.c.project_id == chunk_t.c.project_id,
                    overlay.c.branch == chunk_t.c.branch,
                    overlay.c.scope == FileScope.OVERLAY.value,
                    overlay.c.path == chunk_t.c.path,
                )
                .correlate(chunk_t),
            )
        )
    return clauses


def _hit(row: Any, *, score: float = 0.0, matches: int = 1, terms: int = 0) -> ChunkHit:
    return ChunkHit(
        path=row.path,
        scope=row.scope,
        language=row.language,
        line_count=row.line_count,
        start_line=row.start_line,
        end_line=row.end_line,
        heading=row.heading,
        text=row.text,
        score=score,
        matches=matches,
        terms_matched=terms,
    )


class ProjectStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    # -- projects ---------------------------------------------------------------------------
    async def get_project(self, project_id: uuid.UUID) -> Project | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(sa.select(project_t).where(project_t.c.id == project_id))
            ).first()
        return _project(row) if row else None

    async def find_by_fingerprint(self, fingerprint: str) -> Project | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    sa.select(project_t).where(project_t.c.fingerprint == fingerprint)
                )
            ).first()
        return _project(row) if row else None

    async def create_project(
        self,
        *,
        name: str,
        fingerprint: str,
        remote: str | None,
        root_commit: str | None,
        local_root: str,
        default_branch: str | None,
    ) -> Project:
        project_id, now = new_id(), utcnow()
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.insert(project_t).values(
                    id=project_id,
                    name=name,
                    fingerprint=fingerprint,
                    remote=remote,
                    root_commit=root_commit,
                    local_root=local_root,
                    default_branch=default_branch,
                    bootstrap_state=BootstrapState.NEW.value,
                    created_at=now,
                    updated_at=now,
                )
            )
            await self._touch_location(conn, project_id, local_root, now)
        got = await self.get_project(project_id)
        if got is None:  # pragma: no cover - inserted a moment ago
            raise RuntimeError("project vanished after insert")
        return got

    @staticmethod
    async def _touch_location(
        conn: AsyncConnection, project_id: uuid.UUID, path: str, now: datetime
    ) -> None:
        await conn.execute(
            pg_insert(location_t)
            .values(
                id=new_id(), project_id=project_id, path=path, first_seen_at=now, last_seen_at=now
            )
            .on_conflict_do_update(
                constraint="uq_location_project_path", set_={"last_seen_at": now}
            )
        )

    async def relocate(self, project_id: uuid.UUID, new_root: str) -> None:
        """The same project (same identity) was found at a different path."""
        now = utcnow()
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(project_t)
                .where(project_t.c.id == project_id)
                .values(local_root=new_root, updated_at=now)
            )
            await self._touch_location(conn, project_id, new_root, now)

    async def update_state(
        self,
        project_id: uuid.UUID,
        state: BootstrapState,
        *,
        branch: str | None = None,
        commit: str | None = None,
        synced: bool = False,
    ) -> None:
        values: dict[str, Any] = {"bootstrap_state": state.value, "updated_at": utcnow()}
        if branch is not None:
            values["last_branch"] = branch
        if commit is not None:
            values["last_commit"] = commit
        if synced:
            values["last_synced_at"] = utcnow()
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(project_t).where(project_t.c.id == project_id).values(**values)
            )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    # -- model-written file summaries -------------------------------------------------------
    async def get_file_summary(self, project_id: uuid.UUID, path: str) -> dict[str, Any] | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    sa.select(summary_t).where(
                        summary_t.c.project_id == project_id, summary_t.c.path == path
                    )
                )
            ).first()
        return dict(row._mapping) if row is not None else None

    async def put_file_summary(
        self,
        project_id: uuid.UUID,
        path: str,
        *,
        content_hash: str,
        summary: dict[str, Any],
        provider: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        values = {
            "content_hash": content_hash, "summary": summary, "provider": provider, "model": model,
            "input_tokens": input_tokens, "output_tokens": output_tokens, "created_at": utcnow(),
        }  # fmt: skip
        async with self._engine.begin() as conn:
            await conn.execute(
                pg_insert(summary_t)
                .values(project_id=project_id, path=path, **values)
                .on_conflict_do_update(index_elements=["project_id", "path"], set_=values)
            )

    async def list_projects(self, *, limit: int = 100) -> list[Project]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.select(project_t).order_by(project_t.c.created_at.desc()).limit(limit)
                )
            ).all()
        return [_project(r) for r in rows]

    async def add_snapshot(
        self,
        project_id: uuid.UUID,
        *,
        branch: str,
        commit_sha: str,
        dirty_paths: int,
        state: BootstrapState,
        files_total: int,
        files_changed: int,
        symbols_total: int,
        duration_ms: int,
    ) -> uuid.UUID:
        snapshot_id = new_id()
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.insert(snapshot_t).values(
                    id=snapshot_id,
                    project_id=project_id,
                    branch=branch,
                    commit_sha=commit_sha,
                    dirty=dirty_paths > 0,
                    dirty_paths=dirty_paths,
                    state=state.value,
                    files_total=files_total,
                    files_changed=files_changed,
                    symbols_total=symbols_total,
                    duration_ms=duration_ms,
                    created_at=utcnow(),
                )
            )
        return snapshot_id

    async def latest_snapshot(self, project_id: uuid.UUID) -> dict[str, Any] | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    sa.select(snapshot_t)
                    .where(snapshot_t.c.project_id == project_id)
                    .order_by(snapshot_t.c.created_at.desc())
                    .limit(1)
                )
            ).first()
        return dict(row._mapping) if row else None

    # -- files / symbols --------------------------------------------------------------------
    async def committed_files(
        self, project_id: uuid.UUID, branch: str
    ) -> dict[str, tuple[uuid.UUID, str]]:
        """path -> (file id, content hash) of the committed index for one branch."""
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.select(file_t.c.path, file_t.c.id, file_t.c.content_hash).where(
                        file_t.c.project_id == project_id,
                        file_t.c.branch == branch,
                        file_t.c.scope == FileScope.COMMITTED.value,
                    )
                )
            ).all()
        return {r.path: (r.id, r.content_hash) for r in rows}

    async def overlay_hashes(
        self, project_id: uuid.UUID, branch: str
    ) -> dict[str, tuple[str, str]]:
        """path -> (content hash, status) of the current uncommitted view of one branch."""
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.select(file_t.c.path, file_t.c.content_hash, file_t.c.status).where(
                        file_t.c.project_id == project_id,
                        file_t.c.branch == branch,
                        file_t.c.scope == FileScope.OVERLAY.value,
                    )
                )
            ).all()
        return {r.path: (r.content_hash, r.status) for r in rows}

    async def coverage_counts(self, project_id: uuid.UUID, branch: str) -> dict[str, Any]:
        """Raw numbers behind semantic coverage for the committed index of one branch."""
        from eios_storage.tables.knowledge import item as knowledge_item

        committed = FileScope.COMMITTED.value
        async with self._engine.connect() as conn:
            by_language = (
                await conn.execute(
                    sa.select(file_t.c.language, sa.func.count())
                    .where(
                        file_t.c.project_id == project_id,
                        file_t.c.branch == branch,
                        file_t.c.scope == committed,
                        file_t.c.status == "active",
                    )
                    .group_by(file_t.c.language)
                )
            ).all()
            with_symbols = (
                await conn.execute(
                    sa.select(sa.func.count(sa.distinct(symbol_t.c.file_id))).where(
                        symbol_t.c.project_id == project_id,
                        symbol_t.c.branch == branch,
                        symbol_t.c.scope == committed,
                    )
                )
            ).scalar_one()
            parse_errors = (
                await conn.execute(
                    sa.select(sa.func.count()).where(
                        file_t.c.project_id == project_id,
                        file_t.c.branch == branch,
                        file_t.c.scope == committed,
                        file_t.c.metadata["parse_error"].astext.is_not(None),
                    )
                )
            ).scalar_one()
            with_knowledge = (
                await conn.execute(
                    sa.select(sa.func.count(sa.distinct(file_t.c.path)))
                    .select_from(
                        file_t.join(
                            knowledge_item,
                            sa.and_(
                                knowledge_item.c.project_id == file_t.c.project_id,
                                knowledge_item.c.metadata["path"].astext == file_t.c.path,
                                knowledge_item.c.health.notin_(["QUARANTINED", "SUPERSEDED"]),
                            ),
                        )
                    )
                    .where(
                        file_t.c.project_id == project_id,
                        file_t.c.branch == branch,
                        file_t.c.scope == committed,
                    )
                )
            ).scalar_one()
            content_files = (
                await conn.execute(
                    sa.select(sa.func.count()).where(
                        file_t.c.project_id == project_id,
                        file_t.c.branch == branch,
                        file_t.c.scope == committed,
                        file_t.c.status == "active",
                        _wants_chunks(),
                    )
                )
            ).scalar_one()
            with_content, content_chunks = (
                await conn.execute(
                    sa.select(sa.func.count(sa.distinct(chunk_t.c.file_id)), sa.func.count()).where(
                        chunk_t.c.project_id == project_id,
                        chunk_t.c.branch == branch,
                        chunk_t.c.scope == committed,
                    )
                )
            ).one()
        return {
            "languages": {lang: int(n) for lang, n in by_language},
            "files_with_symbols": int(with_symbols),
            "files_with_knowledge": int(with_knowledge),
            "content_files": int(content_files),
            "files_with_content": int(with_content),
            "content_chunks": int(content_chunks),
            "parse_errors": int(parse_errors),
        }

    async def count_chunks(self, project_id: uuid.UUID, branch: str, scope: FileScope) -> int:
        async with self._engine.connect() as conn:
            return int(
                (
                    await conn.execute(
                        sa.select(sa.func.count()).where(
                            chunk_t.c.project_id == project_id,
                            chunk_t.c.branch == branch,
                            chunk_t.c.scope == scope.value,
                        )
                    )
                ).scalar_one()
            )

    async def paths_needing_chunks(self, project_id: uuid.UUID, branch: str) -> set[str]:
        """Committed files whose text should be in the content index but is not at the current
        :data:`CHUNK_VERSION` (indexed before the content index existed, or by an older chunker)."""
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.select(file_t.c.path).where(
                        file_t.c.project_id == project_id,
                        file_t.c.branch == branch,
                        file_t.c.scope == FileScope.COMMITTED.value,
                        file_t.c.status == "active",
                        _wants_chunks(),
                        sa.func.coalesce(file_t.c.metadata["chunk_version"].astext, "")
                        != str(CHUNK_VERSION),
                    )
                )
            ).all()
        return {r.path for r in rows}

    async def symbol_names_for_files(self, file_ids: Sequence[uuid.UUID]) -> set[str]:
        """Lower-cased names of all symbols currently stored for the given files."""
        names: set[str] = set()
        async with self._engine.connect() as conn:
            for chunk in _chunks(list(file_ids), 1000):
                rows = (
                    await conn.execute(
                        sa.select(symbol_t.c.name_lower)
                        .where(symbol_t.c.file_id.in_(list(chunk)))
                        .distinct()
                    )
                ).all()
                names.update(r[0] for r in rows)
        return names

    async def provides_index(self, project_id: uuid.UUID, branch: str) -> dict[str, list[str]]:
        """module key -> files providing it (committed scope)."""
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.select(file_t.c.path, file_t.c.metadata["provides"].label("provides")).where(
                        file_t.c.project_id == project_id,
                        file_t.c.branch == branch,
                        file_t.c.scope == FileScope.COMMITTED.value,
                        file_t.c.status == "active",
                    )
                )
            ).all()
        index: dict[str, list[str]] = {}
        for row in rows:
            for key in row.provides or []:
                index.setdefault(key, []).append(row.path)
        return index

    async def apply_changes(
        self,
        project_id: uuid.UUID,
        branch: str,
        scope: FileScope,
        changes: Sequence[FileChange],
        deleted_paths: Sequence[str] = (),
        *,
        expires_at: datetime | None = None,
    ) -> None:
        """Atomically upsert ``changes`` and remove ``deleted_paths`` (rows, symbols, graph)."""
        now = utcnow()
        sc = scope.value
        async with self._engine.begin() as conn:
            gone = list(deleted_paths) + [c.path for c in changes]
            for chunk in _chunks(gone, 500):
                await self._delete_owned_graph(conn, project_id, branch, sc, list(chunk))
            if deleted_paths:
                for chunk in _chunks(list(deleted_paths), 500):
                    await conn.execute(
                        sa.delete(file_t).where(
                            file_t.c.project_id == project_id,
                            file_t.c.branch == branch,
                            file_t.c.scope == sc,
                            file_t.c.path.in_(list(chunk)),
                        )
                    )
            node_rows: dict[str, dict[str, Any]] = {}
            edge_rows: dict[tuple[str, str, str], dict[str, Any]] = {}
            symbol_rows: list[dict[str, Any]] = []
            chunk_rows: list[dict[str, Any]] = []
            for change in changes:
                file_id = await self._upsert_file(
                    conn, project_id, branch, sc, change, now, expires_at
                )
                chunk_rows.extend(
                    {
                        "id": new_id(),
                        "file_id": file_id,
                        "project_id": project_id,
                        "branch": branch,
                        "scope": sc,
                        "path": change.path,
                        "ordinal": c.ordinal,
                        "start_line": c.start_line,
                        "end_line": c.end_line,
                        "heading": c.heading,
                        "text": c.text,
                        "idents": list(c.idents),
                    }
                    for c in change.chunks
                )
                symbol_rows.extend(
                    {
                        "id": new_id(),
                        "file_id": file_id,
                        "project_id": project_id,
                        "branch": branch,
                        "scope": sc,
                        "name": s.name,
                        "qualified_name": s.qualified_name,
                        "kind": s.kind,
                        "container": s.container,
                        "start_line": s.start_line,
                        "end_line": s.end_line,
                        "signature": s.signature[:500],
                        "exported": s.exported,
                    }
                    for s in change.symbols
                )
                for n in change.nodes:
                    node_rows[n.key] = {
                        "id": new_id(),
                        "project_id": project_id,
                        "branch": branch,
                        "scope": sc,
                        "key": n.key,
                        "kind": n.kind,
                        "label": n.label[:500],
                        "path": n.path,
                        "attrs": n.attrs,
                    }
                for e in change.edges:
                    edge_rows[(e.src_key, e.dst_key, e.kind)] = self._edge_row(
                        project_id, branch, sc, e
                    )
            for chunk in _chunks(symbol_rows):
                await conn.execute(sa.insert(symbol_t), list(chunk))
            for chunk in _chunks(chunk_rows, _CHUNK_INSERT):
                await conn.execute(sa.insert(chunk_t), list(chunk))
            for chunk in _chunks(list(node_rows.values())):
                stmt = pg_insert(node_t).values(list(chunk))
                await conn.execute(
                    stmt.on_conflict_do_update(
                        constraint="uq_node_key",
                        set_={
                            "kind": stmt.excluded.kind,
                            "label": stmt.excluded.label,
                            "path": stmt.excluded.path,
                            "attrs": stmt.excluded.attrs,
                        },
                    )
                )
            for chunk in _chunks(list(edge_rows.values())):
                await conn.execute(
                    pg_insert(edge_t)
                    .values(list(chunk))
                    .on_conflict_do_nothing(constraint="uq_edge")
                )

    @staticmethod
    def _edge_row(project_id: uuid.UUID, branch: str, sc: str, e: GraphEdgeSpec) -> dict[str, Any]:
        return {
            "id": new_id(),
            "project_id": project_id,
            "branch": branch,
            "scope": sc,
            "src_key": e.src_key,
            "dst_key": e.dst_key,
            "kind": e.kind,
            "owner_path": e.owner_path,
            "weight": e.weight,
            "confidence": e.confidence,
            "attrs": e.attrs,
        }

    @staticmethod
    async def _delete_owned_graph(
        conn: AsyncConnection, project_id: uuid.UUID, branch: str, sc: str, paths: list[str]
    ) -> None:
        if not paths:
            return
        await conn.execute(
            sa.delete(edge_t).where(
                edge_t.c.project_id == project_id,
                edge_t.c.branch == branch,
                edge_t.c.scope == sc,
                edge_t.c.owner_path.in_(paths),
            )
        )
        await conn.execute(
            sa.delete(node_t).where(
                node_t.c.project_id == project_id,
                node_t.c.branch == branch,
                node_t.c.scope == sc,
                node_t.c.path.in_(paths),
            )
        )

    @staticmethod
    async def _upsert_file(
        conn: AsyncConnection,
        project_id: uuid.UUID,
        branch: str,
        sc: str,
        change: FileChange,
        now: datetime,
        expires_at: datetime | None,
    ) -> uuid.UUID:
        values = {
            "language": change.language,
            "size_bytes": change.size_bytes,
            "line_count": change.line_count,
            "content_hash": change.content_hash,
            "status": change.status,
            "indexed_at": now,
            "expires_at": expires_at,
            "metadata": change.metadata,
        }
        existing = (
            await conn.execute(
                sa.select(file_t.c.id).where(
                    file_t.c.project_id == project_id,
                    file_t.c.branch == branch,
                    file_t.c.scope == sc,
                    file_t.c.path == change.path,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            await conn.execute(sa.delete(symbol_t).where(symbol_t.c.file_id == existing))
            await conn.execute(sa.delete(chunk_t).where(chunk_t.c.file_id == existing))
            await conn.execute(sa.update(file_t).where(file_t.c.id == existing).values(**values))
            return uuid.UUID(str(existing))
        file_id = new_id()
        await conn.execute(
            sa.insert(file_t).values(
                id=file_id,
                project_id=project_id,
                branch=branch,
                scope=sc,
                path=change.path,
                **values,
            )
        )
        return file_id

    async def clear_overlay(self, project_id: uuid.UUID, branch: str) -> int:
        """Delete the whole overlay (uncommitted view) of one branch. Returns files removed."""
        sc = FileScope.OVERLAY.value
        async with self._engine.begin() as conn:
            for table in (edge_t, node_t):
                await conn.execute(
                    sa.delete(table).where(
                        table.c.project_id == project_id,
                        table.c.branch == branch,
                        table.c.scope == sc,
                    )
                )
            res = await conn.execute(
                sa.delete(file_t).where(
                    file_t.c.project_id == project_id,
                    file_t.c.branch == branch,
                    file_t.c.scope == sc,
                )
            )
        return int(res.rowcount)

    async def purge_expired_overlays(self, *, now: datetime | None = None) -> int:
        """Remove overlay files past their TTL (with their symbols, nodes and edges)."""
        now = now or utcnow()
        sc = FileScope.OVERLAY.value
        removed = 0
        async with self._engine.begin() as conn:
            rows = (
                await conn.execute(
                    sa.select(file_t.c.project_id, file_t.c.branch, file_t.c.path).where(
                        file_t.c.scope == sc, file_t.c.expires_at <= now
                    )
                )
            ).all()
            by_target: dict[tuple[uuid.UUID, str], list[str]] = {}
            for r in rows:
                by_target.setdefault((r.project_id, r.branch), []).append(r.path)
            for (project_id, branch), paths in by_target.items():
                await self._delete_owned_graph(conn, project_id, branch, sc, paths)
                res = await conn.execute(
                    sa.delete(file_t).where(
                        file_t.c.project_id == project_id,
                        file_t.c.branch == branch,
                        file_t.c.scope == sc,
                        file_t.c.path.in_(paths),
                    )
                )
                removed += int(res.rowcount)
        return removed

    # -- queries ----------------------------------------------------------------------------
    async def list_files(
        self,
        project_id: uuid.UUID,
        branch: str,
        *,
        scope: FileScope = FileScope.COMMITTED,
        after_path: str | None = None,
        limit: int = 200,
        language: str | None = None,
        path_prefix: str | None = None,
    ) -> list[FileRow]:
        stmt = (
            sa.select(file_t)
            .where(
                file_t.c.project_id == project_id,
                file_t.c.branch == branch,
                file_t.c.scope == scope.value,
            )
            # bytewise ("C") collation: stable cursor pagination independent of the DB locale
            .order_by(file_t.c.path.collate("C"))
            .limit(max(1, min(limit, 1000)))
        )
        if after_path:
            stmt = stmt.where(file_t.c.path.collate("C") > after_path)
        if language:
            stmt = stmt.where(file_t.c.language == language)
        if path_prefix:
            stmt = stmt.where(
                file_t.c.path.like(path_prefix.replace("%", r"\%") + "%", escape="\\")
            )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [FileRow(**dict(r._mapping)) for r in rows]

    async def count_files(self, project_id: uuid.UUID, branch: str, scope: FileScope) -> int:
        async with self._engine.connect() as conn:
            return int(
                (
                    await conn.execute(
                        sa.select(sa.func.count())
                        .select_from(file_t)
                        .where(
                            file_t.c.project_id == project_id,
                            file_t.c.branch == branch,
                            file_t.c.scope == scope.value,
                        )
                    )
                ).scalar_one()
            )

    async def count_symbols(self, project_id: uuid.UUID, branch: str, scope: FileScope) -> int:
        async with self._engine.connect() as conn:
            return int(
                (
                    await conn.execute(
                        sa.select(sa.func.count())
                        .select_from(symbol_t)
                        .where(
                            symbol_t.c.project_id == project_id,
                            symbol_t.c.branch == branch,
                            symbol_t.c.scope == scope.value,
                        )
                    )
                ).scalar_one()
            )

    async def find_symbols(
        self,
        project_id: uuid.UUID,
        branch: str,
        names_lower: Sequence[str],
        *,
        scopes: Sequence[FileScope] = (FileScope.COMMITTED,),
        kinds: Sequence[str] | None = None,
        limit: int = 500,
        by_qualified: bool = False,
    ) -> list[SymbolRow]:
        """Symbols by lower-cased name, or by lower-cased *qualified* name (``by_qualified``)."""
        if not names_lower:
            return []
        column = sa.func.lower(symbol_t.c.qualified_name) if by_qualified else symbol_t.c.name_lower
        stmt = (
            sa.select(symbol_t, file_t.c.path)
            .select_from(symbol_t.join(file_t, file_t.c.id == symbol_t.c.file_id))
            .where(
                symbol_t.c.project_id == project_id,
                symbol_t.c.branch == branch,
                symbol_t.c.scope.in_([s.value for s in scopes]),
                column.in_(list(names_lower)),
            )
            .order_by(file_t.c.path, symbol_t.c.start_line)
            .limit(limit)
        )
        if kinds:
            stmt = stmt.where(symbol_t.c.kind.in_(list(kinds)))
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [
            SymbolRow(
                **{
                    k: v
                    for k, v in r._mapping.items()
                    if k not in {"name_lower", "project_id", "branch"}
                }
            )
            for r in rows
        ]

    async def search_symbols(
        self,
        project_id: uuid.UUID,
        branch: str,
        query: str,
        *,
        scopes: Sequence[FileScope] = (FileScope.COMMITTED,),
        limit: int = 50,
    ) -> list[SymbolRow]:
        """Match on symbol name or qualified name (case-insensitive).

        Ranking: exact name, name prefix, name substring, then qualified-name-only matches (so
        ``userservice`` also finds ``UserService.create``).
        """
        q = query.lower().strip()
        if not q:
            return []
        escaped = q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        qualified_lower = sa.func.lower(symbol_t.c.qualified_name)
        rank = sa.case(
            (symbol_t.c.name_lower == q, 0),
            (symbol_t.c.name_lower.like(escaped + "%", escape="\\"), 1),
            (symbol_t.c.name_lower.like("%" + escaped + "%", escape="\\"), 2),
            else_=3,
        )
        stmt = (
            sa.select(symbol_t, file_t.c.path)
            .select_from(symbol_t.join(file_t, file_t.c.id == symbol_t.c.file_id))
            .where(
                symbol_t.c.project_id == project_id,
                symbol_t.c.branch == branch,
                symbol_t.c.scope.in_([s.value for s in scopes]),
                sa.or_(
                    symbol_t.c.name_lower.like("%" + escaped + "%", escape="\\"),
                    qualified_lower.like("%" + escaped + "%", escape="\\"),
                ),
            )
            .order_by(rank, sa.func.length(symbol_t.c.qualified_name), file_t.c.path)
            .limit(max(1, min(limit, 500)))
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [
            SymbolRow(
                **{
                    k: v
                    for k, v in r._mapping.items()
                    if k not in {"name_lower", "project_id", "branch"}
                }
            )
            for r in rows
        ]

    async def symbol_match_counts(
        self,
        project_id: uuid.UUID,
        branch: str,
        terms: Sequence[str],
        *,
        scopes: Sequence[FileScope] = (FileScope.COMMITTED,),
    ) -> dict[str, int]:
        """How many indexed symbols have each term in their name: a document frequency, in one
        query, so a retriever can drop terms too common to discriminate."""
        if not terms:
            return {}
        columns = [
            sa.func.count()
            .filter(symbol_t.c.name_lower.like("%" + _like_escape(t) + "%", escape="\\"))
            .label(f"t{n}")
            for n, t in enumerate(terms)
        ]
        stmt = sa.select(*columns).where(
            symbol_t.c.project_id == project_id,
            symbol_t.c.branch == branch,
            symbol_t.c.scope.in_([s.value for s in scopes]),
            symbol_t.c.kind.notin_(("section", "variable", "field")),
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(stmt)).one()
        return {t: int(row[n]) for n, t in enumerate(terms)}

    async def search_paths(
        self,
        project_id: uuid.UUID,
        branch: str,
        terms: Sequence[str],
        *,
        scopes: Sequence[FileScope] = (FileScope.COMMITTED,),
        limit: int = 400,
    ) -> list[FileRow]:
        """Live files whose path contains any of ``terms`` (lower-case substrings)."""
        clean = [t for t in terms if t]
        if not clean:
            return []
        stmt = (
            sa.select(file_t)
            .where(
                file_t.c.project_id == project_id,
                file_t.c.branch == branch,
                file_t.c.scope.in_([s.value for s in scopes]),
                file_t.c.status != "deleted",
                sa.or_(
                    *(
                        sa.func.lower(file_t.c.path).like("%" + _like_escape(t) + "%", escape="\\")
                        for t in clean
                    )
                ),
            )
            .order_by(file_t.c.path.collate("C"))
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [FileRow(**dict(r._mapping)) for r in rows]

    async def find_files(
        self,
        project_id: uuid.UUID,
        branch: str,
        paths: Sequence[str],
        *,
        scopes: Sequence[FileScope] = (FileScope.COMMITTED,),
    ) -> list[FileRow]:
        """Files whose path equals, or ends with ``/<path>`` for, any of ``paths``."""
        clean = [p.strip("/") for p in paths if p.strip("/")]
        if not clean:
            return []
        suffixes = [
            file_t.c.path.like(
                "%/" + p.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_"), escape="\\"
            )
            for p in clean
        ]
        stmt = (
            sa.select(file_t)
            .where(
                file_t.c.project_id == project_id,
                file_t.c.branch == branch,
                file_t.c.scope.in_([s.value for s in scopes]),
                sa.or_(file_t.c.path.in_(clean), *suffixes),
            )
            .order_by(file_t.c.path.collate("C"))
            .limit(200)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [FileRow(**dict(r._mapping)) for r in rows]

    async def find_paths_containing(
        self,
        project_id: uuid.UUID,
        branch: str,
        fragments: Sequence[str],
        *,
        also_any: Sequence[str] = (),
        scopes: Sequence[FileScope] = (FileScope.COMMITTED,),
        limit: int = 500,
    ) -> list[FileRow]:
        """Files whose lower-cased path contains any of ``fragments`` and, when ``also_any`` is
        given, any of those too."""

        def contains_any(words: Sequence[str]) -> sa.ColumnElement[bool]:
            return sa.or_(
                *(
                    sa.func.lower(file_t.c.path).like(f"%{_like_escape(w.lower())}%", escape="\\")
                    for w in words
                )
            )

        if not [f for f in fragments if f.strip()]:
            return []
        stmt = (
            sa.select(file_t)
            .where(
                file_t.c.project_id == project_id,
                file_t.c.branch == branch,
                file_t.c.scope.in_([s.value for s in scopes]),
                contains_any([f for f in fragments if f.strip()]),
            )
            .order_by(file_t.c.path.collate("C"))
            .limit(limit)
        )
        if also_any:
            stmt = stmt.where(contains_any(also_any))
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [FileRow(**dict(r._mapping)) for r in rows]

    # -- content (searchable file text) --------------------------------------------------------
    async def search_content(
        self,
        project_id: uuid.UUID,
        branch: str,
        words: Sequence[str],
        identifiers: Sequence[str] = (),
        *,
        scopes: Sequence[FileScope] = (FileScope.COMMITTED,),
        limit: int = 10,
        max_terms: int = 10,
        identifier_bonus: float = 2.5,
        min_terms: int = 2,
        relative_floor: float = 0.3,
    ) -> list[ChunkHit]:
        """The best chunk of each file for a natural-language question, best file first.

        A chunk scores the rarity (inverse document frequency over the project's chunks) of each
        question word it contains, more when the word is in its heading or its file's path, plus
        a bonus per exact identifier it mentions. A word in half of all chunks counts for nothing,
        which is what lets a long question full of everyday words still find the one chunk that
        holds its two rare ones. A chunk must hold ``min_terms`` of the words (or one that is in
        only a handful of chunks, or an exact identifier) and reach ``relative_floor`` of the best
        score: one stray everyday word is not a hit.
        """
        clean = [w for w in dict.fromkeys(words) if w.isascii() and w.isalnum()]
        idents = list(dict.fromkeys(identifiers))[:20]
        if not clean and not idents:
            return []
        vec = chunk_t.c.search_vector
        base = [
            chunk_t.c.project_id == project_id,
            chunk_t.c.branch == branch,
            *_chunk_visible(scopes),
        ]
        async with self._engine.connect() as conn:
            lexeme_rows = (
                await conn.execute(
                    sa.text(
                        "SELECT t, to_tsquery('english', t)::text"
                        " FROM unnest(CAST(:terms AS text[])) AS t"
                    ),
                    {"terms": clean},
                )
            ).all()
            lexemes: dict[str, str] = {row[0]: row[1] for row in lexeme_rows}
            unique: dict[str, str] = {}  # one word per stem: "event" and "events" count once
            for word in clean:
                if lexemes.get(word):
                    unique.setdefault(lexemes[word], word)
            terms = list(unique.values())
            weights: list[tuple[str, float]] = []
            rare: set[str] = set()
            if terms:
                counts = (
                    await conn.execute(
                        sa.select(
                            sa.func.count(),
                            *(
                                sa.func.count().filter(
                                    vec.op("@@")(sa.func.to_tsquery("english", t))
                                )
                                for t in terms
                            ),
                        ).where(*base)
                    )
                ).one()
                total = int(counts[0])
                for t, df in zip(terms, counts[1:], strict=True):
                    if 0 < df <= total * _CONTENT_GENERIC:
                        weights.append((t, math.log(1 + (total - df + 0.5) / (df + 0.5))))
                        if df <= _CONTENT_RARE:
                            rare.add(t)
                weights = sorted(weights, key=lambda w: -w[1])[:max_terms]
            if not weights and not idents:
                return []
            parts: list[sa.ColumnElement[Any]] = []
            present: list[sa.ColumnElement[Any]] = []
            present_rare: list[sa.ColumnElement[Any]] = []
            for t, idf in weights:
                anywhere = vec.op("@@")(sa.func.to_tsquery("english", t))
                present.append(sa.case((anywhere, 1), else_=0))
                if t in rare:
                    present_rare.append(sa.case((anywhere, 1), else_=0))
                parts.append(sa.case((anywhere, idf), else_=0.0))
                in_heading = vec.op("@@")(sa.func.to_tsquery("english", f"{t}:A"))
                parts.append(sa.case((in_heading, 0.5 * idf), else_=0.0))
                in_path = vec.op("@@")(sa.func.to_tsquery("english", f"{t}:C"))
                parts.append(sa.case((in_path, 0.4 * idf), else_=0.0))
            matches: list[sa.ColumnElement[bool]] = []
            if weights:
                query = sa.func.to_tsquery("english", " | ".join(t for t, _ in weights))
                matches.append(vec.op("@@")(query))
                proximity: sa.ColumnElement[Any] = sa.func.ts_rank_cd(vec, query)
            else:
                proximity = sa.literal(0.0)
            ident_hit: sa.ColumnElement[Any] = sa.literal(0)
            if idents:
                matches.append(chunk_t.c.idents.overlap(idents))
                named = chunk_t.c.idents.overlap(idents)
                parts.append(sa.case((named, identifier_bonus), else_=0.0))
                ident_hit = sa.case((named, 1), else_=0)
            score = sum(parts[1:], parts[0])
            matched = sum(present[1:], present[0]) if present else sa.literal(0)
            rare_matched = sum(present_rare[1:], present_rare[0]) if present_rare else sa.literal(0)
            inner = (
                sa.select(
                    chunk_t.c.path,
                    chunk_t.c.scope,
                    file_t.c.language,
                    file_t.c.line_count,
                    chunk_t.c.start_line,
                    chunk_t.c.end_line,
                    chunk_t.c.heading,
                    chunk_t.c.text,
                    score.label("score"),
                    proximity.label("proximity"),
                    matched.label("terms_matched"),
                    rare_matched.label("rare_matched"),
                    ident_hit.label("ident_hit"),
                    sa.func.count().over(partition_by=chunk_t.c.path).label("matches"),
                )
                .select_from(chunk_t.join(file_t, file_t.c.id == chunk_t.c.file_id))
                .where(*base, sa.or_(*matches))
                .ext(distinct_on(chunk_t.c.path))
                .order_by(chunk_t.c.path, sa.desc("score"), sa.desc("proximity"), chunk_t.c.ordinal)
                .subquery()
            )
            rows = (
                await conn.execute(
                    sa.select(inner)
                    .order_by(inner.c.score.desc(), inner.c.matches.desc(), inner.c.path)
                    .limit(max(1, min(limit, 100)))
                )
            ).all()
        floor = relative_floor * (float(rows[0].score) if rows else 0.0)
        need = max(1, min(min_terms, len(weights)))
        return [
            _hit(r, score=float(r.score), matches=int(r.matches), terms=int(r.terms_matched))
            for r in rows
            if float(r.score) >= floor
            and (int(r.terms_matched) >= need or int(r.rare_matched) or int(r.ident_hit))
        ]

    async def search_content_paths(
        self,
        project_id: uuid.UUID,
        branch: str,
        *,
        terms: Sequence[str] = (),
        numbers: Sequence[int] = (),
        scopes: Sequence[FileScope] = (FileScope.COMMITTED,),
        limit: int = 400,
    ) -> list[ChunkHit]:
        """Files of the content index whose path contains a term (lower-case substring) or is the
        ADR/decision with one of ``numbers``; each with its first chunk."""
        likes = [
            sa.func.lower(chunk_t.c.path).like("%" + _like_escape(t) + "%", escape="\\")
            for t in terms
            if t
        ]
        for n in numbers:
            likes.append(
                chunk_t.c.path.op("~*")(
                    rf"(^|/)((adrs?|decisions?|rfcs?)/|adr[-_ ]?)0*{int(n)}[-_. ]"
                )
            )
        if not likes:
            return []
        stmt = (
            sa.select(
                chunk_t.c.path,
                chunk_t.c.scope,
                file_t.c.language,
                file_t.c.line_count,
                chunk_t.c.start_line,
                chunk_t.c.end_line,
                chunk_t.c.heading,
                chunk_t.c.text,
            )
            .select_from(chunk_t.join(file_t, file_t.c.id == chunk_t.c.file_id))
            .where(
                chunk_t.c.project_id == project_id,
                chunk_t.c.branch == branch,
                chunk_t.c.ordinal == 0,  # one row per file: its first chunk
                *_chunk_visible(scopes),
                sa.or_(*likes),
            )
            .order_by(chunk_t.c.path)
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [_hit(r) for r in rows]

    async def find_identifier_files(
        self,
        project_id: uuid.UUID,
        branch: str,
        names: Sequence[str],
        *,
        scopes: Sequence[FileScope] = (FileScope.COMMITTED,),
        per_name_chunks: int = 300,
    ) -> dict[str, list[ChunkHit]]:
        """name -> the files whose text mentions it as a whole word (case-sensitive), each with the
        first chunk that does and the number of chunks that do. Indexed (GIN on the identifier
        array), bounded by ``per_name_chunks`` rows per name."""
        found: dict[str, list[ChunkHit]] = {}
        async with self._engine.connect() as conn:
            for name in dict.fromkeys(names):
                rows = (
                    await conn.execute(
                        sa.select(
                            chunk_t.c.path,
                            chunk_t.c.scope,
                            file_t.c.language,
                            file_t.c.line_count,
                            chunk_t.c.start_line,
                            chunk_t.c.end_line,
                            chunk_t.c.heading,
                            chunk_t.c.text,
                            chunk_t.c.ordinal,
                        )
                        .select_from(chunk_t.join(file_t, file_t.c.id == chunk_t.c.file_id))
                        .where(
                            chunk_t.c.project_id == project_id,
                            chunk_t.c.branch == branch,
                            *_chunk_visible(scopes),
                            chunk_t.c.idents.contains([name]),
                        )
                        .order_by(chunk_t.c.path.collate("C"), chunk_t.c.ordinal)
                        .limit(per_name_chunks)
                    )
                ).all()
                per_path: dict[str, list[Any]] = defaultdict(list)
                for row in rows:
                    per_path[row.path].append(row)
                found[name] = [_hit(group[0], matches=len(group)) for group in per_path.values()]
        return found

    async def file_symbols(self, file_id: uuid.UUID) -> list[SymbolRow]:
        stmt = (
            sa.select(symbol_t, file_t.c.path)
            .select_from(symbol_t.join(file_t, file_t.c.id == symbol_t.c.file_id))
            .where(symbol_t.c.file_id == file_id)
            .order_by(symbol_t.c.start_line)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [
            SymbolRow(
                **{
                    k: v
                    for k, v in r._mapping.items()
                    if k not in {"name_lower", "project_id", "branch"}
                }
            )
            for r in rows
        ]

    # -- graph ------------------------------------------------------------------------------
    async def edges_from(
        self,
        project_id: uuid.UUID,
        branch: str,
        keys: Sequence[str],
        *,
        kinds: Sequence[str] | None = None,
        scopes: Sequence[FileScope] = (FileScope.COMMITTED,),
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        return await self._edges(project_id, branch, "src_key", keys, kinds, scopes, limit)

    async def edges_to(
        self,
        project_id: uuid.UUID,
        branch: str,
        keys: Sequence[str],
        *,
        kinds: Sequence[str] | None = None,
        scopes: Sequence[FileScope] = (FileScope.COMMITTED,),
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        return await self._edges(project_id, branch, "dst_key", keys, kinds, scopes, limit)

    async def in_degrees(
        self,
        project_id: uuid.UUID,
        branch: str,
        keys: Sequence[str],
        *,
        kinds: Sequence[str] | None = None,
        scopes: Sequence[FileScope] = (FileScope.COMMITTED,),
    ) -> dict[str, int]:
        """How many edges point at each key (fan-in: a high count marks a hub such as settings)."""
        if not keys:
            return {}
        stmt = (
            sa.select(edge_t.c.dst_key, sa.func.count())
            .where(
                edge_t.c.project_id == project_id,
                edge_t.c.branch == branch,
                edge_t.c.scope.in_([s.value for s in scopes]),
                edge_t.c.dst_key.in_(list(keys)),
            )
            .group_by(edge_t.c.dst_key)
        )
        if kinds:
            stmt = stmt.where(edge_t.c.kind.in_(list(kinds)))
        async with self._engine.connect() as conn:
            return {key: int(n) for key, n in (await conn.execute(stmt)).all()}

    async def _edges(
        self,
        project_id: uuid.UUID,
        branch: str,
        column: str,
        keys: Sequence[str],
        kinds: Sequence[str] | None,
        scopes: Sequence[FileScope],
        limit: int,
    ) -> list[dict[str, Any]]:
        if not keys:
            return []
        stmt = (
            sa.select(edge_t)
            .where(
                edge_t.c.project_id == project_id,
                edge_t.c.branch == branch,
                edge_t.c.scope.in_([s.value for s in scopes]),
                edge_t.c[column].in_(list(keys)),
            )
            .limit(limit)
        )
        if kinds:
            stmt = stmt.where(edge_t.c.kind.in_(list(kinds)))
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [dict(r._mapping) for r in rows]

    async def nodes_by_keys(
        self,
        project_id: uuid.UUID,
        branch: str,
        keys: Sequence[str],
        *,
        scopes: Sequence[FileScope] = (FileScope.COMMITTED,),
    ) -> list[dict[str, Any]]:
        if not keys:
            return []
        stmt = sa.select(node_t).where(
            node_t.c.project_id == project_id,
            node_t.c.branch == branch,
            node_t.c.scope.in_([s.value for s in scopes]),
            node_t.c.key.in_(list(keys)),
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [dict(r._mapping) for r in rows]

    async def unresolved_and_affected_edges(
        self,
        project_id: uuid.UUID,
        branch: str,
        *,
        kinds: Sequence[str],
        callee_names: Sequence[str] = (),
        dst_keys: Sequence[str] = (),
        exclude_owner_paths: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """Committed edges whose resolution may be stale after files changed."""
        conds: list[sa.ColumnElement[bool]] = []
        if callee_names:
            conds.append(edge_t.c.attrs["callee"].astext.in_(list(callee_names)))
        if dst_keys:
            conds.append(edge_t.c.dst_key.in_(list(dst_keys)))
        if not conds:
            return []
        stmt = sa.select(edge_t).where(
            edge_t.c.project_id == project_id,
            edge_t.c.branch == branch,
            edge_t.c.scope == FileScope.COMMITTED.value,
            edge_t.c.kind.in_(list(kinds)),
            sa.or_(*conds),
        )
        if exclude_owner_paths:
            stmt = stmt.where(edge_t.c.owner_path.notin_(list(exclude_owner_paths)))
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [dict(r._mapping) for r in rows]

    async def edges_with_external_import_keys(
        self,
        project_id: uuid.UUID,
        branch: str,
        keys: Sequence[str],
        *,
        exclude_owner_paths: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """Import edges currently pointing at ``external:*`` whose candidate keys now exist."""
        if not keys:
            return []
        stmt = sa.select(edge_t).where(
            edge_t.c.project_id == project_id,
            edge_t.c.branch == branch,
            edge_t.c.scope == FileScope.COMMITTED.value,
            edge_t.c.kind == "imports",
            edge_t.c.dst_key.like("external:%"),
            sa.func.jsonb_exists_any(
                edge_t.c.attrs["keys"], sa.cast(list(keys), sa.ARRAY(sa.Text))
            ),
        )
        if exclude_owner_paths:
            stmt = stmt.where(edge_t.c.owner_path.notin_(list(exclude_owner_paths)))
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [dict(r._mapping) for r in rows]

    async def replace_edges(
        self,
        project_id: uuid.UUID,
        branch: str,
        scope: FileScope,
        delete_ids: Sequence[uuid.UUID],
        new_edges: Sequence[GraphEdgeSpec],
    ) -> None:
        async with self._engine.begin() as conn:
            for chunk in _chunks(list(delete_ids), 1000):
                await conn.execute(sa.delete(edge_t).where(edge_t.c.id.in_(list(chunk))))
            rows = {
                (e.src_key, e.dst_key, e.kind): self._edge_row(project_id, branch, scope.value, e)
                for e in new_edges
            }
            for chunk in _chunks(list(rows.values())):
                await conn.execute(
                    pg_insert(edge_t)
                    .values(list(chunk))
                    .on_conflict_do_nothing(constraint="uq_edge")
                )

    async def neighborhood(
        self,
        project_id: uuid.UUID,
        branch: str,
        key: str,
        *,
        scopes: Sequence[FileScope] = (FileScope.COMMITTED,),
        limit: int = 200,
    ) -> dict[str, Any]:
        """One-hop neighbourhood of a node key (both directions) for incremental graph loading."""
        out_edges = await self.edges_from(project_id, branch, [key], scopes=scopes, limit=limit)
        in_edges = await self.edges_to(project_id, branch, [key], scopes=scopes, limit=limit)
        keys = {key} | {e["dst_key"] for e in out_edges} | {e["src_key"] for e in in_edges}
        nodes = await self.nodes_by_keys(project_id, branch, sorted(keys), scopes=scopes)
        return {
            "nodes": nodes,
            "edges": out_edges + in_edges,
            "truncated": len(out_edges) >= limit or len(in_edges) >= limit,
        }
