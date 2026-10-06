from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.errors import DomainError
from eios_domain.knowledge import (
    Decision,
    DecisionCreate,
    DecisionStatus,
    Health,
    HumanApproval,
    KnowledgeItemCreate,
    ProvenanceInput,
    SourceKind,
    Trust,
    TrustViolationError,
)
from eios_domain.registry import RegistryState
from eios_domain.vault import Vault
from eios_governance.evaluation import load_suites
from eios_runtime import Container, build_container
from tests.conftest import make_settings
from tests.evals.corpus import build_corpus
from tests.gitfixtures import commit_all

pytestmark = pytest.mark.integration
REPO = Path(__file__).resolve().parents[2]
HUMAN = HumanApproval(approver="human:alice", scope="test")


@pytest.fixture
async def c(db: AsyncEngine, tmp_path: Path) -> Container:
    c = build_container(
        make_settings(
            manifests_dir=REPO / "manifests",
            evals_dir=REPO / "evals",
            blob_dir=tmp_path / "b",
            workspace_roots=str(tmp_path / "ws"),
        ),
        db,
    )
    assert (await c.sync_registries()).ok
    return c


async def add_item(
    c: Container,
    *,
    title: str = "Retry rule",
    content: str = "Retries stop after 3 attempts.",
    trust: Trust = Trust.OBSERVED,
    source: SourceKind = SourceKind.LLM,
    subject: str | None = None,
    project_id: uuid.UUID | None = None,
    health: Health = Health.UNVERIFIED,
) -> uuid.UUID:
    item = await c.knowledge.add_item(
        KnowledgeItemCreate(
            vault=Vault.PROJECT if project_id else Vault.DEFAULT,
            project_id=project_id,
            title=title,
            content=content,
            trust=trust,
            health=health,
            source_kind=source,
            created_by="agent",
            subject_key=subject,
            provenance=[ProvenanceInput(source="test", actor="agent")]
            if source is not SourceKind.MANUAL
            else [],
        )
    )
    return item.id


async def evidence(
    c: Container, source: SourceKind, project_id: uuid.UUID | None = None
) -> uuid.UUID:
    r = await c.pipeline.ingest_raw(
        b"3 passed", source_kind=source, tool_id="pytest", project_id=project_id, summary="run"
    )
    return r.id


async def test_llm_assertion_cannot_reach_verified_without_independent_evidence(
    c: Container,
) -> None:
    item = await add_item(c)
    with pytest.raises(TrustViolationError):
        await c.governance.promote_trust(item, Trust.VERIFIED, actor="agent")
    llm_ev = await evidence(c, SourceKind.LLM)
    with pytest.raises(TrustViolationError):
        await c.governance.promote_trust(item, Trust.VERIFIED, actor="agent", evidence_ids=[llm_ev])
    with pytest.raises(TrustViolationError, match="does not exist"):
        await c.governance.promote_trust(
            item, Trust.VERIFIED, actor="a", evidence_ids=[uuid.uuid4()]
        )
    tool_ev = await evidence(c, SourceKind.TOOL)
    out = await c.governance.promote_trust(item, Trust.VERIFIED, actor="ci", evidence_ids=[tool_ev])
    assert out.trust is Trust.VERIFIED
    with pytest.raises(TrustViolationError):
        await c.governance.promote_trust(
            item, Trust.APPROVED, actor="agent", evidence_ids=[tool_ev]
        )
    approved = await c.governance.promote_trust(item, Trust.APPROVED, actor="alice", human=HUMAN)
    assert approved.trust is Trust.APPROVED
    history = await c.governance.store.history("item", item)
    assert [h["to_value"] for h in history][:2] == ["APPROVED", "VERIFIED"]
    assert history[0]["approval"]["approver"] == "human:alice"


async def test_evidence_from_another_project_does_not_count(
    c: Container, db: AsyncEngine, tmp_path: Path
) -> None:
    ws = tmp_path / "ws"
    corpus = await build_corpus(c, db, ws)
    item = await add_item(c, project_id=corpus.project_id, title="Project fact")
    foreign = await evidence(c, SourceKind.TOOL, project_id=None)
    with pytest.raises(TrustViolationError):
        await c.governance.promote_trust(item, Trust.VERIFIED, actor="x", evidence_ids=[foreign])
    mine = await evidence(c, SourceKind.TOOL, project_id=corpus.project_id)
    assert (
        await c.governance.promote_trust(item, Trust.VERIFIED, actor="x", evidence_ids=[mine])
    ).trust is Trust.VERIFIED


async def test_demoting_to_raw_clears_current_health(c: Container) -> None:
    item = await add_item(c, trust=Trust.DERIVED, source=SourceKind.CODE, health=Health.CURRENT)
    out = await c.governance.promote_trust(item, Trust.RAW, actor="x", reason="source retracted")
    assert out.trust is Trust.RAW and out.health is Health.UNVERIFIED


async def test_health_transitions_and_quarantine_exit_needs_a_human(c: Container) -> None:
    item = await add_item(c, trust=Trust.DERIVED, source=SourceKind.CODE)
    await c.governance.set_health(item, Health.QUARANTINED, actor="sec", reason="suspicious source")
    with pytest.raises(TrustViolationError):
        await c.governance.set_health(item, Health.UNVERIFIED, actor="agent", reason="looks fine")
    out = await c.governance.set_health(
        item, Health.UNVERIFIED, actor="alice", reason="reviewed", human=HUMAN
    )
    assert out.health is Health.UNVERIFIED


async def test_supersession_rules(c: Container) -> None:
    old, new = await add_item(c, title="old"), await add_item(c, title="new")
    with pytest.raises(DomainError):
        await c.governance.supersede(old, old, actor="x", reason="r")
    out = await c.governance.supersede(old, new, actor="x", reason="newer")
    assert out.health is Health.SUPERSEDED and out.superseded_by == new
    with pytest.raises(DomainError, match="already superseded"):
        await c.governance.supersede(old, new, actor="x", reason="again")
    with pytest.raises(DomainError, match=r"cycle|successor"):
        await c.governance.supersede(new, old, actor="x", reason="loop")
    approved = await add_item(c, title="approved", trust=Trust.APPROVED, source=SourceKind.MANUAL)
    with pytest.raises(TrustViolationError):
        await c.governance.supersede(approved, new, actor="agent", reason="r")
    assert (
        await c.governance.supersede(approved, new, actor="alice", reason="r", human=HUMAN)
    ).health is Health.SUPERSEDED


async def test_contradiction_marks_the_less_trusted_side_and_a_human_resolves_it(
    c: Container,
) -> None:
    weak = await add_item(c, title="a", content="limit is 3", trust=Trust.OBSERVED)
    strong = await add_item(
        c, title="b", content="limit is 5", trust=Trust.DERIVED, source=SourceKind.CODE
    )
    cid = await c.governance.report_contradiction(
        weak, strong, reason="different limits", actor="agent"
    )
    assert cid is not None
    assert (
        await c.governance.report_contradiction(strong, weak, reason="dup", actor="agent") is None
    )
    assert (await c.knowledge.knowledge.get(weak)).health is Health.CONTRADICTED  # type: ignore[union-attr]
    assert (await c.knowledge.knowledge.get(strong)).health is not Health.CONTRADICTED  # type: ignore[union-attr]
    with pytest.raises(TrustViolationError):
        await c.governance.set_health(weak, Health.CURRENT, actor="agent", reason="x")
    await c.governance.resolve_contradiction(
        cid, winner=strong, human=HUMAN, resolution="code is truth"
    )
    loser = await c.knowledge.knowledge.get(weak)
    assert loser is not None and loser.health is Health.SUPERSEDED and loser.superseded_by == strong
    assert await c.governance.store.contradictions(status="open") == []


async def test_detect_contradictions_by_subject_key(c: Container) -> None:
    a = await add_item(c, title="a", content="timeout is 30s", subject="cfg.timeout")
    b = await add_item(c, title="b", content="timeout is 60s", subject="cfg.timeout")
    same = await add_item(c, title="c", content="timeout   IS 30s", subject="cfg.other")
    other = await add_item(c, title="d", content="timeout is 30s", subject="cfg.other")
    report = await c.maintenance.run()
    assert report.contradictions_found == 1 and report.items_examined >= 4
    assert (await c.knowledge.knowledge.get(a)) is not None
    open_pairs = {
        frozenset((str(r["item_a"]), str(r["item_b"])))
        for r in await c.governance.store.contradictions()
    }
    assert open_pairs == {frozenset((str(a), str(b)))}
    assert same and other


async def test_sync_invalidates_dependent_knowledge_transitively(
    c: Container, db: AsyncEngine, tmp_path: Path
) -> None:
    ws = tmp_path / "ws"
    corpus = await build_corpus(c, db, ws)
    base = await add_item(
        c,
        project_id=corpus.project_id,
        title="retry facts",
        trust=Trust.DERIVED,
        source=SourceKind.CODE,
        health=Health.CURRENT,
    )
    derived = await add_item(
        c,
        project_id=corpus.project_id,
        title="built on retry facts",
        trust=Trust.DERIVED,
        source=SourceKind.CODE,
        health=Health.CURRENT,
    )
    unrelated = await add_item(
        c,
        project_id=corpus.project_id,
        title="unrelated",
        trust=Trust.DERIVED,
        source=SourceKind.CODE,
        health=Health.CURRENT,
    )
    await c.governance.link_dependencies(base, files=[("shop/payments/retry.py", None)])
    await c.governance.link_dependencies(derived, items=[base])
    await c.governance.link_dependencies(unrelated, files=[("shop/auth/tokens.py", None)])
    repo = ws / "shop"
    (repo / "shop" / "payments" / "retry.py").write_text("def should_retry():\n    return False\n")
    commit_all(repo, "change")
    await c.projects.sync_now(corpus.project_id)
    health = {i: (await c.knowledge.knowledge.get(i)).health for i in (base, derived, unrelated)}  # type: ignore[union-attr]
    assert health[base] is Health.STALE and health[derived] is Health.STALE
    assert health[unrelated] is Health.CURRENT
    why = (await c.governance.store.history("item", base))[0]
    assert "retry.py" in why["reason"] and why["actor"] == "governance"


async def test_recheck_catches_missed_changes_using_recorded_hashes(
    c: Container, db: AsyncEngine, tmp_path: Path
) -> None:
    corpus = await build_corpus(c, db, tmp_path / "ws")
    item = await add_item(
        c,
        project_id=corpus.project_id,
        trust=Trust.DERIVED,
        source=SourceKind.CODE,
        health=Health.CURRENT,
    )
    await c.governance.link_dependencies(
        item, files=[("shop/payments/retry.py", "0" * 64), ("gone.py", None)]
    )
    report = await c.maintenance.run(corpus.project_id)
    assert report.stale_marked == 1 and report.projects_checked == 1
    assert (await c.knowledge.knowledge.get(item)).health is Health.STALE  # type: ignore[union-attr]


async def test_decision_ledger_lifecycle(c: Container) -> None:
    async def mk(q: str, sel: str = "yes") -> Decision:
        return await c.knowledge.record_decision(
            DecisionCreate(question=q, selected=sel, decider="agent", vault=Vault.DEFAULT)
        )

    d1, d2, d3 = (
        await mk("Use retries?"),
        await mk("Use jitter?"),
        await mk("Pick backoff?", sel=""),
    )
    assert d1.status is DecisionStatus.PROPOSED
    with pytest.raises(DomainError, match="selected"):
        await c.decisions.accept(d3.id, HUMAN)
    with pytest.raises(DomainError, match="reason"):
        await c.decisions.reject(d3.id, HUMAN, "")
    assert (await c.decisions.accept(d1.id, HUMAN, "ok")).status is DecisionStatus.ACCEPTED
    with pytest.raises(DomainError, match="PROPOSED"):
        await c.decisions.accept(d1.id, HUMAN)
    with pytest.raises(DomainError, match="ACCEPTED"):
        await c.decisions.supersede(d1.id, d2.id, HUMAN)  # d2 still proposed
    await c.decisions.accept(d2.id, HUMAN)
    out = await c.decisions.supersede(d1.id, d2.id, HUMAN, "better")
    assert out.status is DecisionStatus.SUPERSEDED and out.superseded_by == d2.id
    with pytest.raises(DomainError):
        await c.decisions.supersede(d2.id, d1.id, HUMAN)
    assert (await c.decisions.reject(d3.id, HUMAN, "not needed")).status is DecisionStatus.REJECTED
    history = await c.decisions.history(d1.id)
    assert next(iter(history))["to_value"].startswith("superseded_by")


async def test_evaluation_promotes_to_verified_but_never_trusted_and_ignores_quarantine(
    c: Container,
) -> None:
    suite = next(s for s in load_suites(REPO / "evals")[0] if s.provider == "sql_analyze")
    await c.registry.transition(
        "provider",
        "sql_analyze",
        "1.0.0",
        RegistryState.EXPERIMENTAL,
        actor="test",
        reason="reset for the test",
    )
    report = await c.evaluation.run_suite(suite, promote=True)
    assert report.score == 1.0 and report.verified and report.samples == 4
    row = await c.registry.store.get_provider("sql_analyze")
    assert row is not None and row.state is RegistryState.VERIFIED  # never TRUSTED by evaluation
    metrics = (await c.registry.store.metrics_for("sql_analyze"))[("sql_analyze", "1.0.0")]
    assert metrics.eval_score == 1.0 and metrics.eval_samples == 4
    await c.registry.transition(
        "provider",
        "sql_analyze",
        "1.0.0",
        RegistryState.QUARANTINED,
        actor="sec",
        reason="incident",
    )
    again = await c.evaluation.run_suite(suite, promote=True)
    assert not again.verified and "QUARANTINED" in again.note
    after = await c.registry.store.get_provider("sql_analyze")
    assert after is not None and after.state is RegistryState.QUARANTINED
    assert len(await c.evaluation.history("sql_analyze")) == 2


async def test_failing_suites_do_not_promote_and_record_weaknesses(c: Container) -> None:
    from eios_governance.evaluation import EvalCase, EvalSuite

    await c.registry.transition(
        "provider", "sql_analyze", "1.0.0", RegistryState.EXPERIMENTAL, actor="test", reason="reset"
    )
    bad = EvalSuite(provider="sql_analyze", capability="sql_analyze", cases=[
        EvalCase(id="good", arguments={"sql": "SELECT 1;"}, expect={"line_count": {"gte": 1}}),
        EvalCase(id="wrong", arguments={"sql": "SELECT 1;"}, expect={"objects": {"length": 99}}),
        EvalCase(id="boom", arguments={"sql": 5}, expect={"line_count": 1}),
    ])  # fmt: skip
    report = await c.evaluation.run_suite(bad, promote=True)
    assert report.passed < 3 and not report.verified and "pass rate" in report.note
    m = (await c.registry.store.metrics_for("sql_analyze"))[("sql_analyze", "1.0.0")]
    assert "wrong" in m.known_weaknesses
    row = await c.registry.store.get_provider("sql_analyze")
    assert row is not None and row.state is RegistryState.EXPERIMENTAL
    with pytest.raises(LookupError):
        await c.evaluation.run_suite(EvalSuite(provider="ghost", capability="x", cases=[
            EvalCase(id="a", expect={"x": 1})]))  # fmt: skip


async def test_evaluation_cannot_make_a_forbidden_or_unapproved_provider_routable(
    c: Container, tmp_path: Path
) -> None:
    from eios_capability import RoutingRequest, RoutingStatus, load_directory
    from eios_domain.registry import Origin

    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "t.yaml").write_text(
        "kind: tool\nid: gen_eval\nversion: 1.0.0\ndescription: Generated tool under evaluation.\n"
        "capabilities: [lint_run]\nentrypoint: builtin:sql_analyze\n"
    )
    await c.registry.sync(load_directory(tmp_path / "m"), origin=Origin.GENERATED)
    from eios_governance.evaluation import EvalCase, EvalSuite

    cases = [
        EvalCase(id=str(i), arguments={"sql": "SELECT 1;"}, expect={"line_count": {"gte": 1}})
        for i in range(4)
    ]
    suite = EvalSuite(provider="gen_eval", capability="lint_run", cases=cases)
    report = await c.evaluation.run_suite(suite, promote=True)
    assert report.score == 1.0 and report.verified is False  # generated needs a human first
    decision = await c.router.resolve(RoutingRequest(capability="lint_run"))
    assert decision.status is RoutingStatus.NO_PROVIDER


async def test_governance_api_requires_a_human_for_privileged_acts(
    db: AsyncEngine, migrated_database_url: str, tmp_path: Path
) -> None:
    from httpx import ASGITransport

    from eios_api.app import create_app

    settings = make_settings(
        database_url=migrated_database_url,
        blob_dir=tmp_path / "b",
        admin_token="adm1n-secret-token",
        evals_dir=REPO / "evals",
        manifests_dir=REPO / "manifests",
    )
    app = create_app(settings, engine=db, readiness_checks=[])
    c: Container = app.state.container
    await c.sync_registries()
    item = await add_item(c, trust=Trust.DERIVED, source=SourceKind.CODE)
    adm = {"X-EIOS-Admin-Token": "adm1n-secret-token"}
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        r = await http.post(f"/governance/items/{item}/trust", json={"target": "VERIFIED"})
        assert r.status_code == 403  # no evidence: refused
        r = await http.post(
            f"/governance/items/{item}/trust", json={"target": "APPROVED", "approver": "alice"}
        )
        assert r.status_code == 401  # human claimed without the token
        r = await http.post(
            f"/governance/items/{item}/trust",
            json={"target": "APPROVED", "approver": "alice"},
            headers={"X-EIOS-Admin-Token": "wrong"},
        )
        assert r.status_code == 401
        r = await http.post(
            f"/governance/items/{item}/trust",
            json={"target": "APPROVED", "approver": "alice"},
            headers=adm,
        )
        assert r.status_code == 200 and r.json()["trust"] == "APPROVED"
        life = (await http.get(f"/governance/items/{item}/lifecycle")).json()
        assert life[0]["to_value"] == "APPROVED" and life[0]["actor"] == "human:alice"

        prop = await http.post("/decisions", json={"question": "Use retries?", "selected": "yes"})
        did = prop.json()["id"]
        assert prop.status_code == 201 and prop.json()["status"] == "proposed"
        assert (
            await http.post(f"/decisions/{did}/accept", json={"approver": "alice"})
        ).status_code == 401
        ok = await http.post(f"/decisions/{did}/accept", json={"approver": "alice"}, headers=adm)
        assert ok.status_code == 200 and ok.json()["status"] == "accepted"
        assert (
            await http.post(f"/decisions/{did}/accept", json={"approver": "alice"}, headers=adm)
        ).status_code == 409
        assert (
            await http.post(f"/decisions/{did}/explode", json={"approver": "alice"}, headers=adm)
        ).status_code == 404
        got = (await http.get(f"/decisions/{did}")).json()
        assert got["history"][0]["actor"] == "human:alice"

        suites = (await http.get("/evaluations/suites")).json()
        assert suites and suites[0]["provider"] == "sql_analyze"
        run = await http.post("/evaluations/run", json={"provider": "sql_analyze"})
        assert run.status_code == 200 and run.json()["reports"][0]["score"] == 1.0
        assert (await http.post("/evaluations/run", json={"provider": "nope"})).status_code == 404
        maint = await http.post("/governance/maintenance", json={"wait": True})
        assert maint.status_code == 202 and "items_examined" in maint.json()
        queued = await http.post("/governance/maintenance", json={})
        assert "job_id" in queued.json()
