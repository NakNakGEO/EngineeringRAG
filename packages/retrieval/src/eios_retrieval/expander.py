"""Graph expansion: pull in code that is connected to the seeds even when it is not similar."""

from __future__ import annotations

import math
import posixpath
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from eios_domain.project import FileScope
from eios_project_intelligence import ProjectService
from eios_project_intelligence.languages import is_test_path
from eios_retrieval.graph import DEPENDENCY_KINDS, walk
from eios_retrieval.models import Candidate, CandidateKind, Source
from eios_retrieval.retrievers import RetrievalContext
from eios_retrieval.terms import query_terms, term_overlap

# A node with more incoming edges than this is a hub: its dependents are not walked.
HUB_IN_DEGREE = 25
MAX_TEST_CANDIDATES = 8


def seed_keys(seeds: Sequence[Candidate]) -> list[str]:
    """Graph node keys for seed candidates (a symbol also seeds its file, for imports)."""
    keys: dict[str, None] = {}
    for cand in seeds:
        if cand.id.startswith(("file:", "symbol:")):
            keys.setdefault(cand.id, None)
            if cand.kind is CandidateKind.SYMBOL and cand.path:
                keys.setdefault(f"file:{cand.path}", None)
    return list(keys)


class GraphExpander:
    def __init__(self, projects: ProjectService) -> None:
        self._projects = projects

    async def expand(
        self,
        rc: RetrievalContext,
        seeds: Sequence[Candidate],
        *,
        depth: int,
        direction: str,
        include_tests: bool = False,
        include_siblings: bool = False,
        limit: int = 60,
    ) -> list[Candidate]:
        if rc.project is None or rc.branch is None or depth <= 0:
            return []
        keys = seed_keys(seeds)
        if not keys:
            return []
        store = self._projects.store
        reached = await walk(
            store,
            rc.project.id,
            rc.branch,
            keys,
            depth=depth,
            direction=direction,
            scopes=rc.scopes,
            hub_limit=HUB_IN_DEGREE,
        )
        extra: dict[str, tuple[int, str, str, float, str | None]] = {
            k: (n.distance, n.via, n.direction, n.confidence, n.parent)
            for k, n in reached.items()
            if n.distance > 0
        }
        if include_siblings:
            dirs = sorted({posixpath.dirname(c.path) for c in seeds if c.path})
            for edge in await store.edges_from(
                rc.project.id,
                rc.branch,
                [f"dir:{d or '.'}" for d in dirs],
                kinds=["contains"],
                scopes=rc.scopes,
            ):
                if edge["dst_key"].startswith("file:") and edge["dst_key"] not in reached:
                    extra.setdefault(
                        edge["dst_key"], (depth + 1, "sibling", "out", 0.5, edge["src_key"])
                    )
        if not extra:
            return []
        nodes = {
            n["key"]: n
            for n in await store.nodes_by_keys(
                rc.project.id, rc.branch, sorted(extra), scopes=rc.scopes
            )
        }
        degrees = await store.in_degrees(
            rc.project.id, rc.branch, sorted(extra), kinds=DEPENDENCY_KINDS, scopes=rc.scopes
        )
        terms = query_terms(rc.query.text)
        # Rank before truncating: nearer first, then by edge confidence discounted for hubs and
        # boosted by words shared with the question - never by name, which would favour whatever
        # sorts first alphabetically. Each named seed gets a fair share of the slots.
        # a file stub adds nothing once symbols of that file are reached (same rule as dedupe)
        symbol_paths = {
            str(n["path"]) for k, n in nodes.items() if k.startswith("symbol:") and n.get("path")
        }
        buckets: dict[str, list[tuple[tuple[object, ...], str]]] = defaultdict(list)
        for key, (distance, _via, _way, confidence, _parent) in extra.items():
            node = nodes.get(key)
            if node is None or node["kind"] == "dir":
                continue
            if key.startswith("file:") and key[len("file:") :] in symbol_paths:
                continue
            fan_in = 1.0 / (1.0 + math.log10(1 + degrees.get(key, 0)))
            relevance = term_overlap(terms, f"{node['label']} {node.get('path') or ''}")
            order = (
                distance,
                -(confidence * fan_in + 0.3 * relevance),
                key.startswith("file:"),
                key,
            )
            buckets[_root_of(key, reached)].append((order, key))
        out: list[Candidate] = []
        for key in _round_robin(buckets):
            distance, via, way, confidence, parent = extra[key]
            cand = self._candidate(
                key, nodes[key], distance, via, way, confidence, len(out) + 1, parent
            )
            if cand is None:
                continue
            if cand.path and is_test_path(cand.path) and not include_tests:
                continue
            out.append(cand)
            if len(out) >= limit:
                break
        return out

    async def tests_for(
        self,
        rc: RetrievalContext,
        seeds: Sequence[Candidate],
        *,
        depth: int = 1,
        limit: int = MAX_TEST_CANDIDATES,
    ) -> list[Candidate]:
        """Test files that depend on the seeds, following only *incoming* edges.

        A pure dependents walk - never out-and-back, which would "find" the tests of whatever
        merely shares a caller - restricted to test paths, bounded, and independent of the
        level's own direction, so "which tests cover X" finds the direct ones at L1 and the ones
        reaching X through its callers when it escalates.
        """
        if rc.project is None or rc.branch is None or depth <= 0:
            return []
        keys = seed_keys(seeds)
        if not keys:
            return []
        store = self._projects.store
        reached = await walk(
            store,
            rc.project.id,
            rc.branch,
            keys,
            depth=depth,
            direction="in",
            scopes=rc.scopes,
            hub_limit=HUB_IN_DEGREE,
        )
        best: dict[str, tuple[int, float, str, str]] = {}  # test file key -> (distance, conf, ...)
        for key, node in reached.items():
            if node.distance == 0:
                continue
            path = key[len("file:") :] if key.startswith("file:") else None
            if path is None and key.startswith("symbol:"):
                path = key[len("symbol:") :].split("::", 1)[0]
            if path is None or not is_test_path(path):
                continue
            file_key = f"file:{path}"
            rank = (node.distance, -node.confidence)
            if file_key not in best or rank < (best[file_key][0], -best[file_key][1]):
                best[file_key] = (node.distance, node.confidence, node.via, node.parent or "")
        if not best:
            return []
        nodes = {
            n["key"]: n
            for n in await store.nodes_by_keys(
                rc.project.id, rc.branch, sorted(best), scopes=rc.scopes
            )
        }
        terms = query_terms(rc.query.text)
        ranked = sorted(
            (k for k in best if k in nodes),
            key=lambda k: (best[k][0], -(best[k][1] + 0.3 * term_overlap(terms, k)), k),
        )
        out: list[Candidate] = []
        for key in ranked[:limit]:
            distance, conf, via, parent = best[key]
            cand = self._candidate(
                key, nodes[key], distance, via, "in", conf, len(out) + 1, parent or None
            )
            if cand is not None:
                out.append(cand)
        return out

    @staticmethod
    def _candidate(
        key: str,
        node: dict[str, Any],
        distance: int,
        via: str,
        way: str,
        confidence: float,
        rank: int,
        parent: str | None,
    ) -> Candidate | None:
        attrs = node.get("attrs") or {}
        path = node.get("path")
        score = confidence * (0.7 ** max(distance - 1, 0))
        common: dict[str, Any] = {
            "source": Source.GRAPH,
            "rank": rank,
            "raw_score": score,
            "distance": distance,
            "via": f"{via}/{way}",
            "scope": node.get("scope"),
        }
        if key.startswith("file:"):
            return Candidate(
                id=key,
                kind=CandidateKind.FILE,
                title=path or key[5:],
                path=path or key[5:],
                metadata={
                    "language": attrs.get("language"),
                    "is_test": is_test_path(path or ""),
                    "parent": parent,
                },
                **common,
            )
        if key.startswith("symbol:"):
            return Candidate(
                id=key,
                kind=CandidateKind.SYMBOL,
                title=f"{node['label']} ({attrs.get('kind', node['kind'])})",
                path=path,
                start_line=attrs.get("start_line"),
                end_line=attrs.get("end_line"),
                metadata={
                    "kind": attrs.get("kind"),
                    "name": str(node["label"]).split(".")[-1],
                    "parent": parent,
                },
                **common,
            )
        return None


def _root_of(key: str, reached: dict[str, Any]) -> str:
    """The seed a graph node was reached from (following parents back to distance 0)."""
    if key not in reached:
        return "~siblings"
    seen: set[str] = set()
    while key in reached and reached[key].distance > 0 and key not in seen:
        seen.add(key)
        parent = reached[key].parent
        if parent is None:
            break
        key = parent
    return key


def _round_robin(buckets: dict[str, list[tuple[tuple[object, ...], str]]]) -> list[str]:
    """Interleave per-seed rankings so one chatty seed cannot take every slot."""
    ordered = [sorted(items) for _, items in sorted(buckets.items())]
    out: list[str] = []
    for rank in range(max((len(b) for b in ordered), default=0)):
        for bucket in ordered:
            if rank < len(bucket):
                out.append(bucket[rank][1])
    return out


def scopes_for(include_overlay: bool) -> tuple[FileScope, ...]:
    return (FileScope.COMMITTED, FileScope.OVERLAY) if include_overlay else (FileScope.COMMITTED,)
