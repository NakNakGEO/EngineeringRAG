"""The retrieval pipeline: scope -> retrieve -> fuse -> graph-expand -> dedupe -> rerank."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field

from eios_core.logging import get_logger
from eios_retrieval.expander import GraphExpander
from eios_retrieval.fusion import HeuristicReranker, Reranker, dedupe, rrf_fuse
from eios_retrieval.models import Candidate, CandidateKind, ContextItem, ContextLevel, Source
from eios_retrieval.retrievers import RetrievalContext, Retriever

_log = get_logger("eios.retrieval")
GRAPH_SEED_COUNT = 8


@dataclass(frozen=True)
class LevelSpec:
    """What one context level is allowed to do."""

    level: ContextLevel
    retrievers: frozenset[str]
    per_source_limit: int
    final_limit: int
    graph_depth: int = 0
    graph_direction: str = "out"
    include_tests: bool = False
    include_siblings: bool = False
    snippet_k: int = 10


LEVELS: dict[ContextLevel, LevelSpec] = {
    ContextLevel.L0: LevelSpec(
        ContextLevel.L0, frozenset({"exact"}), per_source_limit=10, final_limit=8, snippet_k=8
    ),
    ContextLevel.L1: LevelSpec(
        ContextLevel.L1,
        frozenset({"exact", "symbol", "fts", "vector"}),
        per_source_limit=10,
        final_limit=15,
        graph_depth=1,
        graph_direction="out",
        snippet_k=12,
    ),
    ContextLevel.L2: LevelSpec(
        ContextLevel.L2,
        frozenset(
            {"exact", "symbol", "fts", "vector", "memory", "decision", "git_history", "reuse"}
        ),
        per_source_limit=15,
        final_limit=30,
        graph_depth=2,
        graph_direction="both",
        snippet_k=20,
    ),
    ContextLevel.L3: LevelSpec(
        ContextLevel.L3,
        frozenset(
            {
                "exact",
                "symbol",
                "fts",
                "vector",
                "memory",
                "decision",
                "git_history",
                "reuse",
                "research",
            }
        ),
        per_source_limit=25,
        final_limit=60,
        graph_depth=3,
        graph_direction="both",
        include_tests=True,
        snippet_k=25,
    ),
    ContextLevel.L4: LevelSpec(
        ContextLevel.L4,
        frozenset(
            {
                "exact",
                "symbol",
                "fts",
                "vector",
                "memory",
                "decision",
                "git_history",
                "reuse",
                "research",
            }
        ),
        per_source_limit=40,
        final_limit=120,
        graph_depth=4,
        graph_direction="both",
        include_tests=True,
        include_siblings=True,
        snippet_k=30,
    ),
}


@dataclass
class PipelineResult:
    items: list[ContextItem]  # ranked; duplicates carry ``redundant_of``
    redundant_ids: list[str]
    produced: dict[str, int] = field(default_factory=dict)  # candidates per retriever
    errors: list[str] = field(default_factory=list)


class RetrievalPipeline:
    def __init__(
        self,
        retrievers: Mapping[str, Retriever],
        expander: GraphExpander | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        self._retrievers = dict(retrievers)
        self._expander = expander
        self._reranker = reranker or HeuristicReranker()

    @property
    def retriever_names(self) -> set[str]:
        return set(self._retrievers)

    async def run(self, rc: RetrievalContext, spec: LevelSpec) -> PipelineResult:
        names = [n for n in sorted(spec.retrievers) if n in self._retrievers]
        results = await asyncio.gather(
            *(self._retrievers[n].retrieve(rc, spec.per_source_limit) for n in names),
            return_exceptions=True,
        )
        batches: dict[str, list[Candidate]] = {}
        errors: list[str] = []
        for name, result in zip(names, results, strict=True):
            if isinstance(result, BaseException):
                _log.warning("retriever_failed", retriever=name, error=repr(result))
                errors.append(f"retriever '{name}' failed: {type(result).__name__}")
                continue
            batches[name] = result
        produced = {n: len(c) for n, c in batches.items()}

        fused = rrf_fuse(batches)
        if self._expander is not None and spec.graph_depth > 0:
            seeds = self._graph_seeds(fused)
            try:
                graph_candidates = await self._expander.expand(
                    rc,
                    seeds,
                    depth=spec.graph_depth,
                    direction=spec.graph_direction,
                    include_tests=spec.include_tests,
                    include_siblings=spec.include_siblings,
                    limit=spec.per_source_limit * 3,
                )
            except Exception as exc:  # graph enrichment must not take the whole request down
                _log.warning("graph_expansion_failed", error=repr(exc))
                errors.append(f"graph expansion failed: {type(exc).__name__}")
                graph_candidates = []
            if graph_candidates:
                batches["graph"] = graph_candidates
                produced["graph"] = len(graph_candidates)
                fused = rrf_fuse(batches)

        deduped, redundant = dedupe(fused)
        ranked = self._reranker.rerank(deduped, rc.query.text, level_rank=spec.level.rank)
        return PipelineResult(ranked, redundant, produced, errors)

    @staticmethod
    def _graph_seeds(fused: list[ContextItem]) -> list[Candidate]:
        code = [
            i
            for i in fused
            if i.candidate.kind in {CandidateKind.SYMBOL, CandidateKind.FILE}
            and i.candidate.source is not Source.GRAPH
        ]
        exact = [i for i in code if Source.EXACT.value in i.sources]
        rest = [i for i in code if Source.EXACT.value not in i.sources]
        return [i.candidate for i in (exact + rest)[: max(GRAPH_SEED_COUNT, len(exact))]]
