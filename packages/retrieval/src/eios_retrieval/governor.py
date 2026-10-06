"""The Context Governor: build a sufficient, evidence-backed context pack for a decision.

Principle (master plan): give the LLM sufficient, high-confidence context for the current
decision; expand automatically when coverage or confidence is inadequate; remove only irrelevant
redundancy. Correctness and coverage outrank token minimisation - the token budget trims the
lowest-value items first, never protected (critical) evidence, and any trimming is reported.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

from eios_domain.events import EventStatus, EventType
from eios_domain.knowledge import SourceKind
from eios_domain.policy import Risk
from eios_domain.project import BootstrapState, FileScope
from eios_knowledge import KnowledgeService, SearchScope
from eios_knowledge.decision_repo import DecisionRepository
from eios_knowledge.memory_repo import MemoryRepository
from eios_knowledge.text import or_query
from eios_observability import RunContext
from eios_project_intelligence import Project, ProjectService
from eios_retrieval.coverage import CoverageEvaluator, Evaluation, resolve_entities
from eios_retrieval.entities import extract_entities
from eios_retrieval.expander import GraphExpander, scopes_for
from eios_retrieval.fusion import Reranker, estimate_tokens
from eios_retrieval.models import (
    Candidate,
    CandidateKind,
    ContextItem,
    ContextLevel,
    ContextPack,
    ExpansionAction,
    RetrievalQuery,
    Source,
)
from eios_retrieval.pipeline import LEVELS, LevelSpec, PipelineResult, RetrievalPipeline
from eios_retrieval.retrievers import (
    DecisionRetriever,
    ExactRetriever,
    FtsRetriever,
    GitHistoryRetriever,
    MemoryRetriever,
    RetrievalContext,
    Retriever,
    SymbolRetriever,
    VectorRetriever,
    default_scope,
)

DEFAULT_TOKEN_BUDGET = 12_000
SNIPPET_MAX_LINES = 60
AUTOMATIC_ACTIONS = frozenset({"raise_level", "fetch_dependencies"})
_START_LEVEL: dict[Risk, ContextLevel] = {
    Risk.LOW: ContextLevel.L1,
    Risk.MEDIUM: ContextLevel.L1,
    Risk.HIGH: ContextLevel.L2,
    Risk.CRITICAL: ContextLevel.L2,
}


def default_retrievers(
    projects: ProjectService,
    knowledge: KnowledgeService,
    memory: MemoryRepository,
    decisions: DecisionRepository,
) -> dict[str, Retriever]:
    return {
        "exact": ExactRetriever(projects),
        "symbol": SymbolRetriever(projects),
        "fts": FtsRetriever(knowledge),
        "vector": VectorRetriever(knowledge),
        "memory": MemoryRetriever(memory, knowledge),
        "decision": DecisionRetriever(decisions),
        "git_history": GitHistoryRetriever(projects),
        "reuse": FtsRetriever(
            knowledge, name="reuse", source=Source.REUSE, kinds=("pattern", "component")
        ),
        "research": FtsRetriever(
            knowledge, name="research", source=Source.RESEARCH, source_kinds=(SourceKind.RESEARCH,)
        ),
    }


def cost_of(item: ContextItem) -> int:
    c = item.candidate
    return estimate_tokens(c.title) + estimate_tokens(c.content) + 16


# Sources that contribute a different *kind* of evidence in small volumes. Each keeps its best
# items in the pack even when high-volume sources would otherwise crowd them out of the cut.
RESERVED_SOURCES = (
    Source.MEMORY.value,
    Source.DECISION.value,
    Source.GIT_HISTORY.value,
    Source.REUSE.value,
    Source.RESEARCH.value,
)
RESERVED_PER_SOURCE = 2


def select_within_budget(
    items: Sequence[ContextItem], budget: int, limit: int
) -> tuple[list[ContextItem], list[ContextItem], bool]:
    """Pick the pack contents.

    1. Critical items (named entities and their direct dependencies) are always kept - even over
       budget or beyond ``limit``.
    2. Each low-volume source (memory, decisions, history, reusable components, research) keeps
       its best :data:`RESERVED_PER_SOURCE` items while they fit the budget.
    3. The remaining slots are filled by score while they fit.

    Returns (selected, dropped, truncated); ``truncated`` is only set when the *budget* (not the
    item limit) excluded something.
    """
    pool = [i for i in items if i.redundant_of is None]
    critical = [i for i in pool if i.critical][: limit * 2]
    selected = list(critical)
    chosen = {i.candidate.id for i in selected}
    used = sum(cost_of(i) for i in critical)
    truncated = used > budget
    dropped: list[ContextItem] = []

    def fits(item: ContextItem) -> bool:
        return used + cost_of(item) <= budget

    for source in RESERVED_SOURCES:
        taken = 0
        for item in pool:
            if taken >= RESERVED_PER_SOURCE:
                break
            if source in item.sources and item.candidate.id not in chosen:
                if fits(item):
                    selected.append(item)
                    chosen.add(item.candidate.id)
                    used += cost_of(item)
                    taken += 1
                else:
                    truncated = True
    slots = max(0, limit - len(selected))
    for item in pool:
        if item.candidate.id in chosen:
            continue
        if slots > 0 and fits(item):
            selected.append(item)
            chosen.add(item.candidate.id)
            used += cost_of(item)
            slots -= 1
        else:
            dropped.append(item)
            if slots > 0:  # the budget, not the item limit, excluded it
                truncated = True
    selected.sort(key=lambda i: -i.score)
    return selected, dropped, truncated


class ContextGovernor:
    def __init__(
        self,
        projects: ProjectService,
        knowledge: KnowledgeService,
        memory: MemoryRepository,
        decisions: DecisionRepository,
        *,
        retrievers: Mapping[str, Retriever] | None = None,
        levels: Mapping[ContextLevel, LevelSpec] | None = None,
        reranker: Reranker | None = None,
        use_graph: bool = True,
    ) -> None:
        self._projects = projects
        self._levels = dict(levels or LEVELS)
        self._pipeline = RetrievalPipeline(
            retrievers or default_retrievers(projects, knowledge, memory, decisions),
            GraphExpander(projects) if use_graph else None,
            reranker,
        )
        self._coverage = CoverageEvaluator(projects)

    # ------------------------------------------------------------------ public API
    async def build(
        self,
        query: RetrievalQuery,
        *,
        token_budget: int = DEFAULT_TOKEN_BUDGET,
        min_level: ContextLevel | None = None,
        max_level: ContextLevel = ContextLevel.L4,
        ctx: RunContext | None = None,
    ) -> ContextPack:
        rc, state = await self._prepare(query)
        resolved = await resolve_entities(self._projects, rc)
        level = min_level or _START_LEVEL[query.risk]
        if level.rank > max_level.rank:
            level = max_level
        tried: list[ContextLevel] = []
        expansions: list[ExpansionAction] = []
        best: ContextPack | None = None

        while True:
            tried.append(level)
            spec = self._levels[level]
            if ctx is not None:
                await ctx.emit(
                    EventType.CONTEXT_REQUESTED,
                    f"context level {level.value}",
                    status=EventStatus.STARTED,
                    data={"level": level.value, "risk": query.risk.value, "budget": token_budget},
                )
                await ctx.emit(
                    EventType.RETRIEVAL_STARTED,
                    f"retrieval at {level.value}",
                    status=EventStatus.STARTED,
                    data={"retrievers": sorted(spec.retrievers), "graph_depth": spec.graph_depth},
                )
            result = await self._pipeline.run(rc, spec)
            pack = await self._assemble(
                rc, spec, result, resolved, state, token_budget, list(tried), list(expansions)
            )
            await self._emit_hits(ctx, pack)
            if best is None or pack.gap.coverage > best.gap.coverage:
                best = pack
            if pack.gap.ready_to_act:
                best = pack
                break
            automatic = [a for a in pack.required_expansions if a.action in AUTOMATIC_ACTIONS]
            nxt = level.next()
            budget_bound = any(m.kind == "budget" for m in pack.gap.missing_context)
            if not automatic or nxt is None or nxt.rank > max_level.rank or budget_bound:
                break
            expansions.extend(automatic)
            if ctx is not None:
                await ctx.emit(
                    EventType.CONTEXT_EXPANDED,
                    f"expanding {level.value} -> {nxt.value}",
                    data={
                        "from": level.value,
                        "to": nxt.value,
                        "actions": [a.model_dump() for a in automatic[:5]],
                        "coverage": pack.gap.coverage,
                    },
                )
            level = nxt

        if best is None:  # pragma: no cover - the loop always assembles at least one pack
            raise RuntimeError("context build produced no pack")
        final = best.model_copy(update={"levels_tried": tried, "expansions": expansions})
        final = final.model_copy(update={"reason": self._reason(final)})
        if ctx is not None:
            await ctx.emit(
                EventType.CONTEXT_REQUESTED,
                f"context ready_to_act={final.gap.ready_to_act} at {final.level.value}",
                data={
                    "level": final.level.value,
                    "ready_to_act": final.gap.ready_to_act,
                    "coverage": final.gap.coverage,
                    "confidence": final.gap.confidence,
                    "items": len(final.items),
                    "missing": [m.kind for m in final.gap.missing_context][:10],
                },
            )
        return final

    async def search(
        self,
        query: RetrievalQuery,
        *,
        level: ContextLevel = ContextLevel.L1,
        limit: int | None = None,
    ) -> PipelineResult:
        """A single retrieval pass (no expansion loop, no budget): for search endpoints."""
        rc, _ = await self._prepare(query)
        spec = self._levels[level]
        if limit is not None:
            spec = LevelSpec(**{**spec.__dict__, "final_limit": limit})
        return await self._pipeline.run(rc, spec)

    # ------------------------------------------------------------------ internals
    async def _prepare(
        self, query: RetrievalQuery
    ) -> tuple[RetrievalContext, BootstrapState | None]:
        project: Project | None = None
        branch: str | None = None
        state: BootstrapState | None = None
        if query.project_id is not None:
            project = await self._projects.require(query.project_id)
            state = project.bootstrap_state if project.last_branch else BootstrapState.NEW
            branch = query.branch or project.last_branch
        scope: SearchScope = default_scope(query)
        rc = RetrievalContext(
            query=query,
            entities=extract_entities(query.text, symbols=query.symbols, paths=query.paths),
            project=project,
            branch=branch,
            scopes=scopes_for(query.include_overlay),
            search_scope=scope,
            or_text=or_query(query.text),
        )
        return rc, state

    async def _assemble(
        self,
        rc: RetrievalContext,
        spec: LevelSpec,
        result: PipelineResult,
        resolved: dict[str, list[str]],
        state: BootstrapState | None,
        budget: int,
        tried: list[ContextLevel],
        expansions: list[ExpansionAction],
    ) -> ContextPack:
        marked = self._mark_critical(result.items)
        selected, dropped, truncated = select_within_budget(marked, budget, spec.final_limit)
        selected = await self._load_snippets(rc, selected, spec.snippet_k)
        # snippets add tokens; trim again if they pushed us over the budget
        selected, more_dropped, snippet_truncated = select_within_budget(
            selected, budget, spec.final_limit
        )
        truncated = truncated or snippet_truncated
        ev: Evaluation = await self._coverage.evaluate(
            rc, selected, resolved, truncated=truncated, project_state=state
        )
        ev.report.notes.extend(result.errors)
        redundant = sorted(
            set(result.redundant_ids) | {i.candidate.id for i in [*dropped, *more_dropped]}
        )
        return ContextPack(
            query=rc.query.text,
            level=spec.level,
            items=selected,
            gap=ev.gap,
            coverage=ev.report,
            required_expansions=ev.required_expansions,
            redundant_context_ids=redundant,
            critical_evidence_ids=ev.critical_ids,
            contradictions=ev.contradictions,
            budget_tokens=budget,
            used_tokens=sum(cost_of(i) for i in selected),
            truncated=truncated,
            levels_tried=tried,
            expansions=expansions,
        )

    @staticmethod
    def _mark_critical(items: list[ContextItem]) -> list[ContextItem]:
        exact_ids = {i.candidate.id for i in items if Source.EXACT.value in i.sources}
        out: list[ContextItem] = []
        for item in items:
            c = item.candidate
            critical = Source.EXACT.value in item.sources or (
                c.distance == 1
                and str(c.via or "").endswith("/out")
                and c.metadata.get("parent") in exact_ids
            )
            out.append(item.model_copy(update={"critical": True}) if critical else item)
        return out

    async def _load_snippets(
        self, rc: RetrievalContext, items: list[ContextItem], k: int
    ) -> list[ContextItem]:
        if rc.project is None or rc.branch is None:
            return items
        project_id = rc.project.id
        targets = [
            i
            for i in items
            if i.candidate.kind in {CandidateKind.SYMBOL, CandidateKind.FILE}
            and i.candidate.path
            and i.candidate.scope is not None
        ][:k]
        sem = asyncio.Semaphore(8)

        async def load(item: ContextItem) -> tuple[str, str] | None:
            c: Candidate = item.candidate
            start = c.start_line or 1
            end = min(c.end_line or (start + SNIPPET_MAX_LINES - 1), start + SNIPPET_MAX_LINES - 1)
            async with sem:
                try:
                    data = await self._projects.read_source(
                        project_id,
                        c.path or "",
                        branch=rc.branch,
                        start_line=start,
                        end_line=end,
                        prefer_overlay=FileScope.OVERLAY in rc.scopes,
                        max_lines=SNIPPET_MAX_LINES,
                    )
                except Exception:  # a missing blob must not break the pack
                    return None
            return (c.id, data["text"]) if data else None

        loaded = {r[0]: r[1] for r in await asyncio.gather(*(load(i) for i in targets)) if r}
        return [
            i.model_copy(
                update={
                    "candidate": i.candidate.model_copy(update={"content": loaded[i.candidate.id]})
                }
            )
            if i.candidate.id in loaded
            else i
            for i in items
        ]

    @staticmethod
    async def _emit_hits(ctx: RunContext | None, pack: ContextPack) -> None:
        if ctx is None:
            return
        kinds = {
            CandidateKind.KNOWLEDGE: EventType.KNOWLEDGE_HIT,
            CandidateKind.MEMORY: EventType.MEMORY_HIT,
        }
        counts: dict[EventType, int] = {}
        for item in pack.items:
            c = item.candidate
            event = EventType.GRAPH_HIT if c.source is Source.GRAPH else kinds.get(c.kind)
            if event is None or counts.get(event, 0) >= 5:
                continue
            counts[event] = counts.get(event, 0) + 1
            await ctx.emit(
                event,
                c.title[:200],
                data={"id": c.id, "score": round(item.score, 5), "sources": item.sources},
            )

    @staticmethod
    def _reason(pack: ContextPack) -> str:
        if pack.gap.ready_to_act:
            return (
                f"coverage {pack.gap.coverage:.2f} meets the threshold at {pack.level.value}; "
                f"{len(pack.items)} items, {pack.used_tokens}/{pack.budget_tokens} tokens"
            )
        blockers = [m.description for m in pack.gap.missing_context if m.blocking] or [
            m.description for m in pack.gap.missing_context
        ][:3]
        return f"not ready at {pack.level.value} (coverage {pack.gap.coverage:.2f}): " + "; ".join(
            blockers[:3]
        )


__all__ = ["ContextGovernor", "default_retrievers", "select_within_budget"]
