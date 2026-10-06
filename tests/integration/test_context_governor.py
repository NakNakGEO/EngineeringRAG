"""Context Governor behaviour on the synthetic shop corpus."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.events import EventType
from eios_domain.ids import new_id
from eios_domain.knowledge import (
    DecisionCreate,
    KnowledgeItemCreate,
    MemoryCreate,
    SourceKind,
    Trust,
)
from eios_domain.policy import Risk
from eios_domain.project import BootstrapState
from eios_domain.vault import Vault
from eios_retrieval import ContextGovernor, ContextLevel, RetrievalQuery
from eios_retrieval.governor import default_retrievers
from eios_retrieval.models import CandidateKind, Source
from eios_runtime import Container, build_container
from tests.conftest import make_settings
from tests.evals.corpus import Corpus, build_corpus
from tests.gitfixtures import commit_all, write

pytestmark = pytest.mark.integration


@pytest.fixture
async def shop(db: AsyncEngine, tmp_path: Path) -> tuple[Container, Corpus, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    container = build_container(
        make_settings(workspace_roots=str(workspace), blob_dir=tmp_path / "blobs"), db
    )
    corpus = await build_corpus(container, db, workspace)
    return container, corpus, workspace / "shop"


def _q(
    corpus: Corpus,
    text: str,
    *,
    risk: Risk = Risk.MEDIUM,
    include_overlay: bool = True,
) -> RetrievalQuery:
    return RetrievalQuery(
        text=text, project_id=corpus.project_id, risk=risk, include_overlay=include_overlay
    )


async def test_named_entity_with_its_dependencies_is_ready_and_carries_source(
    shop: tuple[Container, Corpus, Path],
) -> None:
    container, corpus, _ = shop
    pack = await container.governor.build(
        _q(corpus, "What does `RetryPolicy.compute_backoff` do?", risk=Risk.LOW)
    )
    assert pack.ready_to_act and pack.gap.coverage >= 0.55
    first = pack.items[0].candidate
    assert first.id == "symbol:shop/payments/retry.py::RetryPolicy.compute_backoff"
    assert "def compute_backoff" in first.content and "now_ms()" in first.content  # real source
    assert "symbol:shop/common/clock.py::now_ms" in pack.critical_evidence_ids
    assert pack.coverage.entities_found >= 1 and pack.coverage.dependency_coverage >= 0.5
    assert pack.used_tokens <= pack.budget_tokens and not pack.truncated


async def test_missing_dependencies_trigger_expansion_and_are_reported(
    shop: tuple[Container, Corpus, Path],
) -> None:
    container, corpus, _ = shop
    run = await container.recorder.start_run(kind="context_build", project_id=corpus.project_id)
    pack = await container.governor.build(
        _q(corpus, "Before I change `PaymentGateway.charge`, what does it rely on?"), ctx=run
    )
    assert pack.levels_tried[0] is ContextLevel.L1 and len(pack.levels_tried) >= 2
    assert any(a.action in {"raise_level", "fetch_dependencies"} for a in pack.expansions)
    assert pack.ready_to_act and pack.coverage.dependency_coverage == 1.0
    ids = {i.candidate.id for i in pack.items}
    assert "symbol:shop/payments/retry.py::RetryPolicy.should_retry" in ids

    events = (await container.events.list_events(run.run_id, limit=500)).items
    types = [e.type for e in events]
    assert EventType.CONTEXT_REQUESTED in types and EventType.RETRIEVAL_STARTED in types
    assert EventType.CONTEXT_EXPANDED in types and EventType.GRAPH_HIT in types
    expanded = next(e for e in events if e.type == EventType.CONTEXT_EXPANDED)
    assert expanded.data["from"] == "L1" and expanded.data["to"] == "L2"


async def test_dependency_gap_blocks_readiness_when_expansion_is_not_allowed(
    shop: tuple[Container, Corpus, Path],
) -> None:
    container, corpus, _ = shop
    pack = await container.governor.build(
        _q(corpus, "Before I change `PaymentGateway.charge`, what does it rely on?"),
        min_level=ContextLevel.L0,
        max_level=ContextLevel.L0,  # forbidden to look beyond the named entity
    )
    assert not pack.ready_to_act
    kinds = {m.kind for m in pack.gap.missing_context}
    assert {"missing_dependency", "insufficient_dependency_coverage"} <= kinds
    assert pack.coverage.dependency_coverage < 0.8
    assert any(a.action == "fetch_dependencies" for a in pack.required_expansions)


async def test_token_budget_trims_filler_but_never_critical_evidence_and_reports_it(
    shop: tuple[Container, Corpus, Path],
) -> None:
    container, corpus, _ = shop
    q = _q(corpus, "How does `OrderService.place_order` reserve inventory and take payment?")
    roomy = await container.governor.build(q, token_budget=40_000)
    tight = await container.governor.build(q, token_budget=500)
    assert (tight.truncated and tight.used_tokens > tight.budget_tokens) or tight.truncated
    critical = set(roomy.critical_evidence_ids)
    assert critical and critical <= {i.candidate.id for i in tight.items}  # protected evidence kept
    assert len(tight.items) < len(roomy.items)
    assert any(m.kind == "budget" for m in tight.gap.missing_context)
    assert tight.gap.confidence < roomy.gap.confidence  # trimming is never hidden
    assert tight.redundant_context_ids  # what was dropped is listed


async def test_uncommitted_changes_are_the_current_truth(
    shop: tuple[Container, Corpus, Path],
) -> None:
    container, corpus, repo = shop
    write(
        repo,
        {
            "shop/payments/retry.py": (repo / "shop/payments/retry.py").read_text()
            + "\n\ndef overlay_only_helper():\n    return 'wip'\n"
        },
    )
    await container.projects.sync_now(corpus.project_id)
    with_overlay = await container.governor.build(
        _q(corpus, "What does `overlay_only_helper` return?", include_overlay=True)
    )
    top = with_overlay.items[0].candidate
    assert (
        top.id.endswith("::overlay_only_helper") and top.scope == "overlay" and "wip" in top.content
    )
    without = await container.governor.build(
        _q(corpus, "What does `overlay_only_helper` return?", include_overlay=False)
    )
    assert not without.ready_to_act
    assert any(m.kind == "unresolved_entity" for m in without.gap.missing_context)


async def test_stale_index_blocks_readiness_for_real_work_but_not_trivial_questions(
    shop: tuple[Container, Corpus, Path],
) -> None:
    container, corpus, repo = shop
    write(repo, {"shop/extra.py": "def extra():\n    return 1\n"})
    commit_all(repo, "move head")
    boot = await container.projects.bootstrap(repo)  # records the STALE state, no sync
    assert boot.state is BootstrapState.STALE
    text = "What does `RetryPolicy.compute_backoff` do?"
    medium = await container.governor.build(_q(corpus, text, risk=Risk.MEDIUM))
    assert not medium.ready_to_act
    assert any(m.kind == "stale_index" and m.blocking for m in medium.gap.missing_context)
    assert any(a.action == "sync_project" for a in medium.required_expansions)
    low = await container.governor.build(_q(corpus, text, risk=Risk.LOW))
    assert low.ready_to_act and any(m.kind == "stale_index" for m in low.gap.missing_context)
    assert low.coverage.index_health < 1.0


async def test_high_risk_requires_verified_or_current_source_evidence(
    shop: tuple[Container, Corpus, Path],
) -> None:
    container, corpus, _ = shop
    # a question answered only by a RAW-trust note, in a project without code evidence
    await container.knowledge.add_item(
        KnowledgeItemCreate(
            vault=Vault.PROJECT,
            project_id=corpus.project_id,
            title="Zebra rollout",
            content="The zebra rollout window is Thursday night.",
            source_kind=SourceKind.LLM,
            created_by="llm",
            trust=Trust.RAW,
            provenance=[{"source": "chat", "actor": "llm"}],  # type: ignore[list-item]
        )
    )
    pack = await container.governor.build(
        _q(corpus, "When is the zebra rollout window?", risk=Risk.CRITICAL)
    )
    assert not pack.ready_to_act
    assert any(m.kind == "no_verified_evidence" for m in pack.gap.missing_context)
    approved = await container.governor.build(
        _q(corpus, "What is the policy on storing card numbers?", risk=Risk.CRITICAL)
    )
    assert approved.coverage.evidence_support == 1.0  # an APPROVED item backs it


async def test_stale_and_contradicting_knowledge_is_flagged_and_ranked_below_current(
    shop: tuple[Container, Corpus, Path],
) -> None:
    container, corpus, _ = shop
    pack = await container.governor.build(
        _q(corpus, "How long is the retry delay between charge attempts?"),
        min_level=ContextLevel.L2,
    )
    titles = [i.candidate.metadata.get("logical_id") for i in pack.items]
    assert "kn:retry" in titles
    if "kn:retry-stale" in titles:
        assert titles.index("kn:retry") < titles.index("kn:retry-stale")
        assert any("payment-retry" in c for c in pack.contradictions)
    # near-duplicate copies are removed, not shown twice
    assert "kn:retry-copy" not in titles


async def test_l2_adds_memory_decisions_and_history(shop: tuple[Container, Corpus, Path]) -> None:
    container, corpus, _ = shop
    pid = corpus.project_id
    await container.knowledge.add_memory(
        MemoryCreate(
            vault=Vault.PROJECT,
            project_id=pid,
            content="Team prefers jittered retry delays",
            created_by="u",
        )
    )
    await container.knowledge.record_decision(
        DecisionCreate(
            project_id=pid, question="Should retry delays use jitter?", selected="yes", decider="a"
        )
    )
    text = "Why do retry delays in `shop/payments/retry.py` use jitter?"
    l1 = await container.governor.build(
        _q(corpus, text), min_level=ContextLevel.L1, max_level=ContextLevel.L1
    )
    l2 = await container.governor.build(
        _q(corpus, text), min_level=ContextLevel.L2, max_level=ContextLevel.L2
    )
    assert {i.candidate.kind for i in l1.items}.isdisjoint(
        {CandidateKind.MEMORY, CandidateKind.DECISION, CandidateKind.COMMIT}
    )
    kinds = {i.candidate.kind for i in l2.items}
    assert {CandidateKind.MEMORY, CandidateKind.DECISION, CandidateKind.COMMIT} <= kinds


async def test_project_knowledge_never_leaks_across_projects(
    shop: tuple[Container, Corpus, Path],
) -> None:
    container, corpus, _ = shop
    other = new_id()
    await container.knowledge.add_item(
        KnowledgeItemCreate(
            vault=Vault.PROJECT,
            project_id=other,
            title="Other company retry secret",
            content="Other company retry policy uses ROT13 backoff secret",
            source_kind=SourceKind.MANUAL,
            created_by="x",
        )
    )
    pack = await container.governor.build(
        _q(corpus, "What is the retry policy secret backoff?"), min_level=ContextLevel.L3
    )
    assert all("Other company" not in i.candidate.title for i in pack.items)
    assert all(
        i.candidate.metadata.get("project_id") in (None, str(corpus.project_id)) for i in pack.items
    )


async def test_a_failing_retriever_degrades_the_pack_but_is_reported(
    shop: tuple[Container, Corpus, Path],
) -> None:
    container, corpus, _ = shop
    retrievers = default_retrievers(
        container.projects,
        container.knowledge,
        container.knowledge.memory,
        container.knowledge.decisions,
    )

    class Broken:
        name = "vector"
        source = Source.VECTOR

        async def retrieve(self, rc: object, limit: int) -> list[object]:
            raise RuntimeError("embedding service down")

    retrievers["vector"] = Broken()  # type: ignore[assignment]
    governor = ContextGovernor(
        container.projects,
        container.knowledge,
        container.knowledge.memory,
        container.knowledge.decisions,
        retrievers=retrievers,
    )
    pack = await governor.build(
        _q(corpus, "What does `RetryPolicy.compute_backoff` do?", risk=Risk.LOW)
    )
    assert pack.items and pack.items[0].candidate.id.endswith("compute_backoff")
    assert any("vector" in note and "failed" in note for note in pack.coverage.notes)


async def test_project_that_was_never_indexed_is_flagged(
    shop: tuple[Container, Corpus, Path], db: AsyncEngine, tmp_path: Path
) -> None:
    container, _, _ = shop
    ws = tmp_path / "workspace"
    from tests.gitfixtures import make_repo

    repo = make_repo(ws, "fresh", {"a.py": "def a():\n    pass\n"})
    boot = await container.projects.bootstrap(repo)
    assert boot.project
    pack = await container.governor.build(
        RetrievalQuery(text="What does `a` do?", project_id=boot.project.id)
    )
    assert not pack.ready_to_act
    assert any(m.kind == "stale_index" for m in pack.gap.missing_context)
    assert any(a.action == "sync_project" for a in pack.required_expansions)


def test_git_is_available_for_the_history_retriever() -> None:
    assert subprocess.run(["git", "--version"], capture_output=True, check=False).returncode == 0  # noqa: S607
