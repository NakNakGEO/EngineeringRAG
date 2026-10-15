from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.knowledge import DecisionCreate
from eios_domain.policy import Risk
from eios_runtime import Container, build_container
from tests.conftest import make_settings
from tests.evals.corpus import Corpus, build_corpus

pytestmark = pytest.mark.integration


@pytest.fixture
async def shop(db: AsyncEngine, tmp_path: Path) -> tuple[Container, Corpus]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    container = build_container(
        make_settings(workspace_roots=str(workspace), blob_dir=tmp_path / "blobs"), db
    )
    return container, await build_corpus(container, db, workspace)


async def test_file_impact_reports_transitive_dependents_callers_and_tests(
    shop: tuple[Container, Corpus],
) -> None:
    container, corpus = shop
    report = await container.impact.analyze(corpus.project_id, paths=["shop/payments/retry.py"])
    dependents = {e.path: e.depth for e in report.dependents}
    assert dependents["shop/payments/gateway.py"] == 1  # imports retry.py
    assert dependents["shop/orders/service.py"] == 2  # imports gateway.py
    assert dependents["shop/api/routes.py"] == 3  # imports service.py
    assert dependents["tests/test_retry.py"] == 1
    callers = {e.label for e in report.callers}
    assert "PaymentGateway.charge" in callers  # calls RetryPolicy.should_retry
    assert "tests/test_retry.py" in report.tests and not report.tests_missing
    callee_paths = {e.path for e in report.callees}
    assert {"shop/common/clock.py", "shop/common/errors.py"} <= callee_paths
    assert "shop/payments/ledger.py" in report.related_modules
    assert report.unresolved == [] and report.confidence > 0.8
    assert report.targets[0] == "file:shop/payments/retry.py"


async def test_symbol_impact_and_unresolved_targets(shop: tuple[Container, Corpus]) -> None:
    container, corpus = shop
    report = await container.impact.analyze(
        corpus.project_id, symbols=["PaymentGateway.charge", "DoesNotExist"]
    )
    assert any(c.label == "OrderService.place_order" for c in report.callers)
    assert any("DoesNotExist" in u for u in report.unresolved)
    assert report.confidence < 1.0
    assert "unresolved" in " ".join(report.risk_factors) or "could not be resolved" in " ".join(
        report.risk_factors
    )


async def test_missing_tests_raise_risk(shop: tuple[Container, Corpus]) -> None:
    container, corpus = shop
    report = await container.impact.analyze(corpus.project_id, paths=["shop/auth/tokens.py"])
    assert report.tests_missing and report.tests == []
    assert any("no tests" in f for f in report.risk_factors)
    assert report.dependents  # used by the checkout route...
    assert report.risk in {Risk.MEDIUM, Risk.HIGH, Risk.CRITICAL}  # ...and untested: never "low"
    covered = await container.impact.analyze(corpus.project_id, paths=["shop/payments/retry.py"])
    assert not covered.tests_missing


async def test_sql_impact_finds_referencing_scripts(shop: tuple[Container, Corpus]) -> None:
    container, corpus = shop
    report = await container.impact.analyze(corpus.project_id, symbols=["ledger_entries"])
    assert any(c.label == "usp_record_charge" for c in report.callers)  # INSERT INTO reference
    assert "db/schema.sql" in report.db_scripts
    assert any("SQL" in f for f in report.risk_factors)


async def test_business_rules_decisions_and_history_are_linked(
    shop: tuple[Container, Corpus],
) -> None:
    container, corpus = shop
    await container.knowledge.record_decision(
        DecisionCreate(
            project_id=corpus.project_id,
            question="Should ledger entries be immutable?",
            selected="yes",
            decider="a",
        )
    )
    report = await container.impact.analyze(corpus.project_id, paths=["shop/payments/ledger.py"])
    assert any("Ledger invariant" in r.title for r in report.business_rules)
    assert any("ledger" in d.title.lower() for d in report.decisions)
    assert report.history and report.history[0].id.startswith("commit:")
    assert any("business rule" in f for f in report.risk_factors)


async def test_overlay_changes_are_part_of_the_analysis_when_requested(
    shop: tuple[Container, Corpus], tmp_path: Path
) -> None:
    container, corpus = shop
    repo = tmp_path / "workspace" / "shop"
    (repo / "shop" / "inventory" / "new_consumer.py").write_text(
        "from shop.payments.ledger import record_charge\n\n\n"
        "def audit():\n    return record_charge(1, 2)\n"
    )
    await container.projects.sync_now(corpus.project_id)
    with_overlay = await container.impact.analyze(
        corpus.project_id, paths=["shop/payments/ledger.py"]
    )
    assert "shop/inventory/new_consumer.py" in {e.path for e in with_overlay.dependents}
    committed_only = await container.impact.analyze(
        corpus.project_id, paths=["shop/payments/ledger.py"], include_overlay=False
    )
    assert "shop/inventory/new_consumer.py" not in {e.path for e in committed_only.dependents}


async def test_nothing_resolvable_gives_zero_confidence(shop: tuple[Container, Corpus]) -> None:
    container, corpus = shop
    report = await container.impact.analyze(corpus.project_id, paths=["nope/missing.py"])
    assert report.targets == [] and report.confidence == 0.0 and report.unresolved
