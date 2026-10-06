"""Resolve call/reference sites to symbols (best effort, with explicit confidence)."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from eios_domain.project import FileScope
from eios_project_intelligence.graph_builder import file_key, symbol_key
from eios_project_intelligence.store import GraphEdgeSpec, ProjectStore, SymbolRow

SQL_KINDS = ("table", "view", "procedure", "function", "trigger")
_NAME_CHUNK = 400


@dataclass(frozen=True)
class CallRequest:
    owner_path: str  # file that contains the call site
    caller: str | None  # qualified name of the enclosing symbol; None = module level
    callee: str
    line: int
    kind: str = "calls"  # "calls" | "references"


async def resolve_calls(
    store: ProjectStore,
    project_id: uuid.UUID,
    branch: str,
    requests: Sequence[CallRequest],
    *,
    imported_paths: dict[str, set[str]],
    scopes: Sequence[FileScope] = (FileScope.COMMITTED,),
    hidden_committed_paths: frozenset[str] = frozenset(),
    scope_label: FileScope = FileScope.COMMITTED,
) -> list[GraphEdgeSpec]:
    """Turn call sites into edges.

    Confidence: same file 0.95 > imported file 0.8 > the only candidate in the project 0.3 >
    unresolved ``callee:<name>`` placeholder 0.2 (kept so a later-added definition can resolve it).
    """
    names = sorted({r.callee.lower() for r in requests})
    by_name: dict[str, list[SymbolRow]] = defaultdict(list)
    for i in range(0, len(names), _NAME_CHUNK):
        rows = await store.find_symbols(
            project_id, branch, names[i : i + _NAME_CHUNK], scopes=scopes, limit=20000
        )
        for row in rows:
            if row.scope == FileScope.COMMITTED.value and row.path in hidden_committed_paths:
                continue  # overridden by the overlay version of the same file
            by_name[row.name.lower()].append(row)

    best: dict[tuple[str, str, str], GraphEdgeSpec] = {}
    for req in requests:
        src = symbol_key(req.owner_path, req.caller) if req.caller else file_key(req.owner_path)
        candidates = by_name.get(req.callee.lower(), [])
        if req.kind == "references":
            candidates = [c for c in candidates if c.kind in SQL_KINDS]
        else:
            candidates = [c for c in candidates if c.kind not in {"section", "variable"}]
        same_file = [c for c in candidates if c.path == req.owner_path]
        imported = [c for c in candidates if c.path in imported_paths.get(req.owner_path, ())]
        if same_file:
            targets, confidence = same_file, 0.95
        elif imported:
            targets, confidence = imported, 0.8
        elif len({c.path for c in candidates}) == 1 and len(candidates) <= 3:
            targets, confidence = candidates, 0.3
        elif req.kind == "references" and candidates:
            targets, confidence = candidates[:1], 0.3
        else:
            targets, confidence = [], 0.2
        attrs = {"callee": req.callee, "line": req.line}
        if not targets:
            specs = [
                GraphEdgeSpec(
                    src,
                    f"callee:{req.callee}",
                    req.kind,
                    req.owner_path,
                    confidence=confidence,
                    attrs=attrs,
                )
            ]
        else:
            specs = [
                GraphEdgeSpec(
                    src,
                    symbol_key(t.path, t.qualified_name),
                    req.kind,
                    req.owner_path,
                    confidence=confidence / max(len(targets), 1)
                    if len(targets) > 1
                    else confidence,
                    attrs=attrs,
                )
                for t in targets[:5]
            ]
        for spec in specs:
            if spec.src_key == spec.dst_key:
                continue
            k = (spec.src_key, spec.dst_key, spec.kind)
            if k not in best or spec.confidence > best[k].confidence:
                best[k] = spec
    return list(best.values())
