"""Graph expansion: pull in code that is connected to the seeds even when it is not similar."""

from __future__ import annotations

import posixpath
from collections.abc import Sequence
from typing import Any

from eios_domain.project import FileScope
from eios_project_intelligence import ProjectService
from eios_project_intelligence.languages import is_test_path
from eios_retrieval.graph import walk
from eios_retrieval.models import Candidate, CandidateKind, Source
from eios_retrieval.retrievers import RetrievalContext


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
        ranked = sorted(extra.items(), key=lambda kv: (kv[1][0], -kv[1][3], kv[0]))
        out: list[Candidate] = []
        for key, (distance, via, way, confidence, parent) in ranked:
            node = nodes.get(key)
            if node is None or node["kind"] == "dir":
                continue
            cand = self._candidate(key, node, distance, via, way, confidence, len(out) + 1, parent)
            if cand is None:
                continue
            if cand.path and is_test_path(cand.path) and not include_tests:
                continue
            out.append(cand)
            if len(out) >= limit:
                break
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


def scopes_for(include_overlay: bool) -> tuple[FileScope, ...]:
    return (FileScope.COMMITTED, FileScope.OVERLAY) if include_overlay else (FileScope.COMMITTED,)
