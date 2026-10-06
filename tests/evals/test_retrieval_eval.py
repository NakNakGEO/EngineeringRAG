"""Phase 4 exit criterion: the adaptive pipeline beats the vector-only baseline.

Five strategies run on the same controlled corpus (see ``corpus.py``). Baselines see every source
file/symbol as a text *chunk* (classic chunk-RAG); the later arms add, one capability at a time:
rank fusion + dedupe + rerank, entity/symbol retrieval + graph expansion, and finally the Context
Governor's coverage-driven expansion loop. Set ``EIOS_WRITE_EVAL_REPORT=1`` to regenerate
``docs/evals/retrieval.md`` from a run.
"""

from __future__ import annotations

import os
from itertools import pairwise
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.policy import Risk
from eios_knowledge import KnowledgeService, SearchScope
from eios_project_intelligence import Project
from eios_retrieval import (
    LEVELS,
    ContextGovernor,
    ContextLevel,
    LevelSpec,
    RetrievalPipeline,
    RetrievalQuery,
)
from eios_retrieval.entities import extract_entities
from eios_retrieval.evaluation import Arm, ArmOutput, ArmReport, EvalCase, run_eval, to_markdown
from eios_retrieval.expander import GraphExpander
from eios_retrieval.governor import default_retrievers
from eios_retrieval.retrievers import (
    ExactRetriever,
    FtsRetriever,
    RetrievalContext,
    Retriever,
    SymbolRetriever,
    VectorRetriever,
)
from eios_runtime import Container, build_container
from tests.conftest import make_settings
from tests.evals.corpus import CHUNK_KIND, Corpus, build_corpus

pytestmark = pytest.mark.integration

K = 10
REPORT = Path(__file__).resolve().parents[2] / "docs" / "evals" / "retrieval.md"


def _rc(case: EvalCase, corpus: Corpus, project: Project) -> RetrievalContext:
    from eios_knowledge.text import or_query

    query = RetrievalQuery(
        text=case.question,
        project_id=corpus.project_id,
        symbols=list(case.symbols),
        paths=list(case.paths),
        risk=Risk.MEDIUM,
    )
    return RetrievalContext(
        query=query,
        entities=extract_entities(query.text, symbols=query.symbols, paths=query.paths),
        project=project,
        branch=corpus.branch,
        search_scope=SearchScope(project_id=corpus.project_id),
        or_text=or_query(query.text),
    )


@pytest.fixture
async def world(db: AsyncEngine, tmp_path: Path) -> tuple[Container, Corpus, Project]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    container = build_container(
        make_settings(workspace_roots=str(workspace), blob_dir=tmp_path / "blobs"), db
    )
    corpus = await build_corpus(container, db, workspace)
    project = await container.projects.require(corpus.project_id)
    return container, corpus, project


async def _arms(container: Container, corpus: Corpus, project: Project) -> dict[str, Arm]:
    knowledge: KnowledgeService = container.knowledge
    projects = container.projects

    vector = VectorRetriever(knowledge)
    fts = FtsRetriever(knowledge)

    async def vector_only(case: EvalCase) -> ArmOutput:
        return ArmOutput(await vector.retrieve(_rc(case, corpus, project), K * 3))

    async def fts_only(case: EvalCase) -> ArmOutput:
        return ArmOutput(await fts.retrieve(_rc(case, corpus, project), K * 3))

    text_spec = LevelSpec(ContextLevel.L1, frozenset({"fts", "vector"}), 30, 30, snippet_k=0)
    text_retrievers: dict[str, Retriever] = {"fts": fts, "vector": vector}
    hybrid_pipe = RetrievalPipeline(text_retrievers)

    async def hybrid(case: EvalCase) -> ArmOutput:
        result = await hybrid_pipe.run(_rc(case, corpus, project), text_spec)
        return ArmOutput([i.candidate for i in result.items if i.redundant_of is None])

    graph_retrievers: dict[str, Retriever] = {
        "fts": fts,
        "vector": vector,
        "exact": ExactRetriever(projects),
        "symbol": SymbolRetriever(projects),
    }
    graph_spec = LevelSpec(
        ContextLevel.L1, frozenset(graph_retrievers), 30, 30, graph_depth=1, snippet_k=0
    )
    graph_pipe = RetrievalPipeline(graph_retrievers, GraphExpander(projects))

    async def hybrid_graph(case: EvalCase) -> ArmOutput:
        result = await graph_pipe.run(_rc(case, corpus, project), graph_spec)
        return ArmOutput([i.candidate for i in result.items if i.redundant_of is None])

    retrievers = default_retrievers(projects, knowledge, knowledge.memory, knowledge.decisions)
    governor = ContextGovernor(
        projects, knowledge, knowledge.memory, knowledge.decisions, retrievers=retrievers,
        levels={lvl: LEVELS[lvl] for lvl in LEVELS},
    )  # fmt: skip

    async def adaptive(case: EvalCase) -> ArmOutput:
        pack = await governor.build(
            RetrievalQuery(
                text=case.question,
                project_id=corpus.project_id,
                symbols=list(case.symbols),
                paths=list(case.paths),
                risk=Risk.MEDIUM,
            ),
            token_budget=40_000,
            min_level=ContextLevel.L1,
        )
        return ArmOutput(
            [i.candidate for i in pack.items], ready=pack.gap.ready_to_act, tokens=pack.used_tokens
        )

    return {
        "vector-only": vector_only,
        "fts-only": fts_only,
        "hybrid": hybrid,
        "hybrid+graph": hybrid_graph,
        "hybrid+graph+coverage-expansion": adaptive,
    }


async def test_adaptive_retrieval_beats_the_vector_only_baseline(
    world: tuple[Container, Corpus, Project],
) -> None:
    container, corpus, project = world
    arms = await _arms(container, corpus, project)
    reports = await run_eval(arms, corpus.cases, k=K)
    by_name = {r.name: r for r in reports}
    table = to_markdown(reports, k=K)
    print("\n" + table)

    if os.environ.get("EIOS_WRITE_EVAL_REPORT") == "1":
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        per_case = _per_case_table(reports)
        REPORT.write_text(
            "# Retrieval evaluation (Phase 4)\n\nGenerated by `tests/evals/test_retrieval_eval.py` "
            f"(`EIOS_WRITE_EVAL_REPORT=1`). k={K}. {len(corpus.cases)} labelled questions over a "
            "synthetic codebase; baselines retrieve code as text chunks.\n\n"
            f"{table}\n\n## Per question (recall@{K})\n\n{per_case}\n",
            encoding="utf-8",
        )

    base = by_name["vector-only"]
    best = by_name["hybrid+graph+coverage-expansion"]
    # the headline criterion
    assert best.recall_at_k > base.recall_at_k, table
    assert best.recall_at_k > by_name["fts-only"].recall_at_k, table  # the strongest baseline too
    assert best.answer_support >= base.answer_support, table
    assert best.graph_coverage > base.graph_coverage, table
    # and each added capability must not make things worse than the one before it
    order = ["vector-only", "hybrid", "hybrid+graph", "hybrid+graph+coverage-expansion"]
    for earlier, later in pairwise(order):
        assert by_name[later].recall_at_k >= by_name[earlier].recall_at_k - 1e-9, (
            earlier,
            later,
            table,
        )
    assert best.graph_coverage >= by_name["hybrid+graph"].graph_coverage - 1e-9
    # quality of the knowledge shown
    assert best.duplicate_rate <= by_name["fts-only"].duplicate_rate, table
    assert best.stale_rate <= base.stale_rate, table
    assert best.symbol_accuracy >= base.symbol_accuracy, table


async def test_honesty_unknown_entities_are_reported_not_papered_over(
    world: tuple[Container, Corpus, Project],
) -> None:
    container, corpus, project = world
    adaptive = (await _arms(container, corpus, project))["hybrid+graph+coverage-expansion"]
    case = next(c for c in corpus.cases if c.id == "unknown-entity")
    out = await adaptive(case)
    assert out.ready is False  # it must not claim readiness for something that does not exist


async def test_context_pack_reports_levels_and_missing_context(
    world: tuple[Container, Corpus, Project],
) -> None:
    container, corpus, _ = world
    pack = await container.governor.build(
        RetrievalQuery(
            text="Explain the behaviour of `QuantumLedgerSynchronizer` during checkout",
            project_id=corpus.project_id,
        )
    )
    assert pack.ready_to_act is False
    kinds = {m.kind for m in pack.gap.missing_context}
    assert "unresolved_entity" in kinds
    assert any(m.blocking for m in pack.gap.missing_context)
    assert any(a.action == "resolve_entity" for a in pack.required_expansions)
    assert pack.gap.confidence < 0.9 and "not ready" in pack.reason


def _per_case_table(reports: list[ArmReport]) -> str:
    names = [r.name for r in reports]
    lines = ["| question | " + " | ".join(names) + " |", "|" + "---|" * (len(names) + 1)]
    for i, case in enumerate(reports[0].cases):
        cells = [f"{r.cases[i].recall_at_k:.2f}" for r in reports]
        lines.append(f"| {case.case_id} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


__all__ = ["CHUNK_KIND"]
