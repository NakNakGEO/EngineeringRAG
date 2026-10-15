"""Read-only views of the code graph for the UI: a whole-project overview and one-file detail.

Both read what the indexer already stored (files, symbols, graph nodes and edges). They never touch
the working tree.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_storage.tables.graph import edge as edge_t
from eios_storage.tables.graph import node as node_t
from eios_storage.tables.source import file as file_t
from eios_storage.tables.source import symbol as symbol_t

COMMITTED, OVERLAY = "committed", "overlay"
RELATION_KINDS = ("imports", "calls", "references")
MAX_SYMBOLS = 400
MAX_EDGES = 600


class GraphView:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def overview(
        self,
        project_id: uuid.UUID,
        branch: str,
        *,
        max_files: int = 15000,
        max_imports: int = 60000,
    ) -> dict[str, Any]:
        """Every active file (uncommitted versions and new files included) with its symbol count,
        the file->file import edges between them, and which paths differ from the commit."""
        base = (file_t.c.project_id == project_id, file_t.c.branch == branch)
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.select(
                        file_t.c.id,
                        file_t.c.path,
                        file_t.c.language,
                        file_t.c.line_count,
                        file_t.c.scope,
                        file_t.c.status,
                    )
                    .where(*base, file_t.c.scope.in_([COMMITTED, OVERLAY]))
                    .order_by(file_t.c.path.collate("C"))
                )
            ).all()
            symbol_counts: dict[Any, int] = dict(
                (
                    await conn.execute(
                        sa.select(symbol_t.c.file_id, sa.func.count())
                        .where(
                            symbol_t.c.project_id == project_id,
                            symbol_t.c.branch == branch,
                            symbol_t.c.scope.in_([COMMITTED, OVERLAY]),
                        )
                        .group_by(symbol_t.c.file_id)
                    )
                ).all()
            )
            imports = (
                await conn.execute(
                    sa.select(edge_t.c.src_key, edge_t.c.dst_key)
                    .where(
                        edge_t.c.project_id == project_id,
                        edge_t.c.branch == branch,
                        edge_t.c.scope.in_([COMMITTED, OVERLAY]),
                        edge_t.c.kind == "imports",
                        edge_t.c.dst_key.like("file:%"),
                    )
                    .limit(max_imports + 1)
                )
            ).all()
        # The uncommitted version of a path replaces its committed one; "deleted" removes it.
        by_path: dict[str, Any] = {}
        for r in rows:
            if r.scope == OVERLAY or r.path not in by_path:
                by_path[r.path] = r
        live = [r for r in by_path.values() if r.status == "active"]
        changed = [{"path": r.path, "status": r.status} for r in rows if r.scope == OVERLAY]
        truncated = len(live) > max_files or len(imports) > max_imports
        seen_edges: set[tuple[str, str]] = set()
        links: list[list[str]] = []
        for s, d in imports[:max_imports]:
            pair = (s.removeprefix("file:"), d.removeprefix("file:"))
            if pair not in seen_edges:
                seen_edges.add(pair)
                links.append(list(pair))
        return {
            "files": [
                {
                    "path": f.path,
                    "language": f.language,
                    "lines": f.line_count,
                    "symbols": int(symbol_counts.get(f.id, 0)),
                }
                for f in live[:max_files]
            ],
            "imports": links,
            "changed": changed,
            "truncated": truncated,
        }

    async def file_detail(
        self, project_id: uuid.UUID, branch: str, path: str
    ) -> dict[str, Any] | None:
        """One file: its symbols, and everything it imports/calls/references or is used by."""
        scopes = [COMMITTED, OVERLAY]
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.select(file_t)
                    .where(
                        file_t.c.project_id == project_id,
                        file_t.c.branch == branch,
                        file_t.c.path == path,
                        file_t.c.scope.in_(scopes),
                    )
                    .order_by(sa.case((file_t.c.scope == OVERLAY, 0), else_=1))
                )
            ).all()
            if not rows:
                return None
            row = rows[0]._mapping
            symbols = (
                await conn.execute(
                    sa.select(
                        symbol_t.c.name,
                        symbol_t.c.qualified_name,
                        symbol_t.c.kind,
                        symbol_t.c.container,
                        symbol_t.c.start_line,
                        symbol_t.c.end_line,
                        symbol_t.c.signature,
                    )
                    .where(symbol_t.c.file_id == row["id"])
                    .order_by(symbol_t.c.start_line)
                    .limit(MAX_SYMBOLS)
                )
            ).all()
            own_keys = [f"file:{path}", *(f"symbol:{path}::{s.qualified_name}" for s in symbols)]
            edge_cols = (
                edge_t.c.src_key,
                edge_t.c.dst_key,
                edge_t.c.kind,
                edge_t.c.confidence,
                edge_t.c.attrs,
            )
            scope_ok = (
                edge_t.c.project_id == project_id,
                edge_t.c.branch == branch,
                edge_t.c.scope.in_(scopes),
                edge_t.c.kind.in_(RELATION_KINDS),
            )
            outgoing = (
                await conn.execute(
                    sa.select(*edge_cols)
                    .where(*scope_ok, edge_t.c.src_key.in_(own_keys))
                    .limit(MAX_EDGES)
                )
            ).all()
            incoming = (
                await conn.execute(
                    sa.select(*edge_cols)
                    .where(*scope_ok, edge_t.c.dst_key.in_(own_keys))
                    .limit(MAX_EDGES)
                )
            ).all()
            own = set(own_keys)
            far = {k for e in (*outgoing, *incoming) for k in (e.src_key, e.dst_key)} - own
            labels = {}
            if far:
                labels = {
                    n.key: n
                    for n in (
                        await conn.execute(
                            sa.select(
                                node_t.c.key, node_t.c.kind, node_t.c.label, node_t.c.path
                            ).where(
                                node_t.c.project_id == project_id,
                                node_t.c.branch == branch,
                                node_t.c.scope.in_(scopes),
                                node_t.c.key.in_(sorted(far)),
                            )
                        )
                    ).all()
                }

        def describe(key: str) -> dict[str, Any]:
            n = labels.get(key)
            if n is not None:
                return {"key": key, "kind": n.kind, "label": n.label, "path": n.path}
            kind = key.split(":", 1)[0]
            return {
                "key": key,
                "kind": kind,
                "label": key.split(":", 1)[-1].split("::")[-1],
                "path": None,
            }

        def shape(e: Any, other: str, direction: str) -> dict[str, Any]:
            own_key = e.src_key if direction == "out" else e.dst_key
            return {
                "direction": direction,
                "kind": e.kind,
                "confidence": e.confidence,
                "own": own_key,
                "line": (e.attrs or {}).get("line"),
                "names": (e.attrs or {}).get("names", []),
                "other": describe(other),
            }

        seen: set[tuple[str, str, str]] = set()
        relations: list[dict[str, Any]] = []
        for direction, group in (("out", outgoing), ("in", incoming)):
            for e in group:
                other = e.dst_key if direction == "out" else e.src_key
                if other in own:  # a symbol of this file talking to another symbol of the same file
                    continue
                ident = (e.src_key, e.dst_key, e.kind)
                if ident in seen:
                    continue
                seen.add(ident)
                relations.append(shape(e, other, direction))
        return {
            "path": path,
            "language": row["language"],
            "lines": row["line_count"],
            "size_bytes": row["size_bytes"],
            "scope": row["scope"],
            "status": row["status"],
            "symbols": [
                {
                    "name": s.name,
                    "qualified_name": s.qualified_name,
                    "kind": s.kind,
                    "container": s.container,
                    "start_line": s.start_line,
                    "end_line": s.end_line,
                    "signature": s.signature,
                }
                for s in symbols
            ],
            "relations": relations,
        }
