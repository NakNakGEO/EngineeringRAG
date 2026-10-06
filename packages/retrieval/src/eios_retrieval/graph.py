"""Bounded breadth-first traversal of the key-based code graph."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from eios_domain.project import FileScope
from eios_project_intelligence.store import ProjectStore

DEPENDENCY_KINDS = ("calls", "imports", "references")
_EXCLUDED_PREFIXES = ("external:", "callee:")


@dataclass
class WalkNode:
    key: str
    distance: int
    via: str  # edge kind that reached it ("seed" for seeds)
    confidence: float  # weakest edge confidence along the path
    direction: str  # "out" (dependency) | "in" (dependent) | "seed"
    parent: str | None = None


async def walk(
    store: ProjectStore,
    project_id: uuid.UUID,
    branch: str,
    seeds: Sequence[str],
    *,
    depth: int,
    direction: str = "out",
    kinds: Sequence[str] = DEPENDENCY_KINDS,
    min_confidence: float = 0.5,
    scopes: Sequence[FileScope] = (FileScope.COMMITTED,),
    max_nodes: int = 600,
) -> dict[str, WalkNode]:
    """Nodes reachable from ``seeds`` within ``depth`` hops.

    ``direction``: ``out`` follows dependencies (what a seed uses), ``in`` follows dependents
    (what uses the seed), ``both`` does both. Unresolved/external targets are never followed.
    Edges below ``min_confidence`` are ignored so guesses do not drag in unrelated code.
    """
    visited: dict[str, WalkNode] = {k: WalkNode(k, 0, "seed", 1.0, "seed") for k in seeds}
    frontier = list(seeds)
    for level in range(1, depth + 1):
        if not frontier or len(visited) >= max_nodes:
            break
        found: dict[str, WalkNode] = {}
        edges: list[tuple[dict[str, Any], str]] = []
        if direction in {"out", "both"}:
            edges += [
                (e, "out")
                for e in await store.edges_from(
                    project_id, branch, frontier, kinds=kinds, scopes=scopes
                )
            ]
        if direction in {"in", "both"}:
            edges += [
                (e, "in")
                for e in await store.edges_to(
                    project_id, branch, frontier, kinds=kinds, scopes=scopes
                )
            ]
        for edge, way in edges:
            if edge["confidence"] < min_confidence:
                continue
            origin, target = (
                (edge["src_key"], edge["dst_key"])
                if way == "out"
                else (edge["dst_key"], edge["src_key"])
            )
            if target in visited or target.startswith(_EXCLUDED_PREFIXES):
                continue
            conf = min(visited[origin].confidence, float(edge["confidence"]))
            current = found.get(target)
            if current is None or conf > current.confidence:
                found[target] = WalkNode(target, level, edge["kind"], conf, way, origin)
        visited.update(found)
        frontier = list(found)
    return visited
