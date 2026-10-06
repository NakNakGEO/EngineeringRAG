"""Persistence for projects, indexed files/symbols and the key-based code graph."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from eios_domain.ids import new_id, utcnow
from eios_domain.project import BootstrapState, FileScope
from eios_storage.tables.graph import edge as edge_t
from eios_storage.tables.graph import node as node_t
from eios_storage.tables.project import location as location_t
from eios_storage.tables.project import project as project_t
from eios_storage.tables.project import snapshot as snapshot_t
from eios_storage.tables.source import file as file_t
from eios_storage.tables.source import symbol as symbol_t

_INSERT_CHUNK = 2000


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


def _chunks(items: Sequence[Any], size: int = _INSERT_CHUNK) -> Iterable[Sequence[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


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
        return {
            "languages": {lang: int(n) for lang, n in by_language},
            "files_with_symbols": int(with_symbols),
            "files_with_knowledge": int(with_knowledge),
            "parse_errors": int(parse_errors),
        }

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
            for change in changes:
                file_id = await self._upsert_file(
                    conn, project_id, branch, sc, change, now, expires_at
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
    ) -> list[SymbolRow]:
        if not names_lower:
            return []
        stmt = (
            sa.select(symbol_t, file_t.c.path)
            .select_from(symbol_t.join(file_t, file_t.c.id == symbol_t.c.file_id))
            .where(
                symbol_t.c.project_id == project_id,
                symbol_t.c.branch == branch,
                symbol_t.c.scope.in_([s.value for s in scopes]),
                symbol_t.c.name_lower.in_(list(names_lower)),
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
