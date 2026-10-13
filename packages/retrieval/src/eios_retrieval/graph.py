"""Bounded breadth-first traversal of the key-based code graph."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from eios_domain.project import FileScope
from eios_project_intelligence.store import ProjectStore

DEPENDENCY_KINDS = ("calls", "imports", "references")
# How many direct dependencies of one named seed are protected in the pack and required by the
# coverage check. The two must agree, or readiness demands what the pack is not allowed to keep.
DEPENDENCIES_PER_SEED = 8
CALLEE_PREFIX = "callee:"
WEAK_CONFIDENCE = 0.2  # confidence of an unresolved ``callee:<name>`` call edge
_EXCLUDED_PREFIXES = ("external:", CALLEE_PREFIX)


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
    hub_limit: int | None = None,
) -> dict[str, WalkNode]:
    """Nodes reachable from ``seeds`` within ``depth`` hops.

    ``direction``: ``out`` follows dependencies (what a seed uses), ``in`` follows dependents
    (what uses the seed), ``both`` does both. Unresolved/external targets are never followed.
    Edges below ``min_confidence`` are ignored so guesses do not drag in unrelated code. With
    ``hub_limit``, a non-seed node with more incoming edges than that is a hub (settings, ids,
    package ``__init__``): it is reported, but its dependents are not walked, since they would
    drown the neighbourhood of what was actually asked about.
    """
    visited: dict[str, WalkNode] = {k: WalkNode(k, 0, "seed", 1.0, "seed") for k in seeds}
    frontier = list(seeds)
    frontier_in = list(seeds)
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
        if direction in {"in", "both"} and frontier_in:
            edges += [
                (e, "in")
                for e in await store.edges_to(
                    project_id, branch, frontier_in, kinds=kinds, scopes=scopes
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
        frontier_in = frontier
        if hub_limit is not None and frontier and direction in {"in", "both"}:
            degrees = await store.in_degrees(
                project_id, branch, frontier, kinds=kinds, scopes=scopes
            )
            frontier_in = [k for k in frontier if degrees.get(k, 0) <= hub_limit]
    return visited


async def unresolved_callers(
    store: ProjectStore,
    project_id: uuid.UUID,
    branch: str,
    names: Sequence[str],
    *,
    scopes: Sequence[FileScope] = (FileScope.COMMITTED,),
    min_confidence: float = WEAK_CONFIDENCE,
    max_nodes: int = 400,
) -> dict[str, WalkNode]:
    """Call sites that use ``names`` but could not be tied to a definition (``callee:<name>``).

    ``walk`` never follows these placeholders, so a caller whose resolution failed is invisible to
    it. The result is name-based guesswork: every entry carries the weak edge confidence and must
    be presented as unverified.
    """
    seeds = [f"{CALLEE_PREFIX}{n}" for n in dict.fromkeys(names)]
    reached = await walk(
        store,
        project_id,
        branch,
        seeds,
        depth=1,
        direction="in",
        kinds=("calls", "references"),
        min_confidence=min_confidence,
        scopes=scopes,
        max_nodes=max_nodes,
    )
    return {k: n for k, n in reached.items() if n.distance > 0}
