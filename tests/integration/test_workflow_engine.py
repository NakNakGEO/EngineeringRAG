from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.policy import Risk
from eios_domain.workflow import NodeStatus, WorkflowStatus
from eios_observability.graph import build_graph
from eios_runtime import Container, build_container
from eios_workflow import TeamSignals, WorkflowError
from eios_workflow.signals import RISK_RANK
from tests.conftest import make_settings
from tests.gitfixtures import make_repo

pytestmark = pytest.mark.integration
EPOCH = datetime.min.replace(tzinfo=UTC)
REPO = Path(__file__).resolve().parents[2]

UNDERSTAND = {"summary": "retry lives in RetryPolicy", "context_confidence": 0.9, "unknowns": []}
DESIGN = {"plan": "extend RetryPolicy.should_retry", "reuse_candidates": []}
IMPLEMENT = {"changed_files": ["shop/payments/retry.py"], "notes": "done"}
CURATE = {"knowledge_updates": [], "outcome": "retry limit raised"}


@pytest.fixture
async def container(db: AsyncEngine, tmp_path: Path) -> Container:
    c = build_container(
        make_settings(manifests_dir=REPO / "manifests", blob_dir=tmp_path / "blobs"), db
    )
    assert (await c.sync_registries()).ok
    return c


@pytest.fixture
async def indexed(db: AsyncEngine, tmp_path: Path) -> tuple[Container, uuid.UUID]:
    """A project where PolicyEngine.evaluate has nine dependent modules and no tests."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    c = build_container(
        make_settings(
            manifests_dir=REPO / "manifests",
            blob_dir=tmp_path / "blobs",
            workspace_roots=str(workspace),
        ),
        db,
    )
    assert (await c.sync_registries()).ok
    files: dict[str, str | bytes] = {
        "app/__init__.py": "",
        "app/policy.py": (
            "class PolicyEngine:\n    def evaluate(self, request):\n        return request\n"
        ),
        "README.md": "# gate\n",
    }
    for i in range(9):
        files[f"app/consumer_{i}.py"] = (
            "from app.policy import PolicyEngine\n\n\n"
            f"def run_{i}(request):\n    return PolicyEngine().evaluate(request)\n"
        )
    repo = make_repo(workspace, "gate", files, remote="https://example.invalid/acme/gate.git")
    boot = await c.projects.bootstrap(repo)
    assert boot.project is not None
    await c.projects.sync_now(boot.project.id)
    return c, boot.project.id


async def start(
    c: Container, risk: Risk = Risk.MEDIUM, goal: str = "Fix the retry limit"
) -> uuid.UUID:
    run = await c.workflows.start(goal=goal, risk=risk)
    return run.id


async def evidence_id(c: Container, project_id: uuid.UUID | None = None) -> uuid.UUID:
    record = await c.pipeline.ingest_raw(
        b"collected 3 tests, 3 passed", project_id=project_id, tool_id="test_run", summary="pytest"
    )
    return record.id


async def test_happy_path_runs_all_five_stages_and_completes_the_run(container: Container) -> None:
    wid = await start(container)
    brief = await container.workflows.brief(wid)
    assert brief["node"]["id"] == "understand" and brief["team"]["writer"] == "software_engineer"
    run = await container.workflows.report(
        wid, node_id="understand", reporter="software_engineer", outputs=UNDERSTAND
    )
    assert run.current_node == "design"
    await container.workflows.report(
        wid, node_id="design", reporter="software_engineer", outputs=DESIGN
    )
    await container.workflows.report(
        wid, node_id="implement", reporter="software_engineer", outputs=IMPLEMENT
    )
    ev = await evidence_id(container)
    await container.workflows.report(
        wid,
        node_id="verify",
        reporter="code_reviewer",
        outputs={"all_passed": True},
        evidence_ids=[ev],
    )
    done = await container.workflows.report(
        wid, node_id="curate", reporter="software_engineer", outputs=CURATE
    )
    assert done.status is WorkflowStatus.COMPLETED and done.current_node is None
    assert [n.status for n in done.node_runs] == [NodeStatus.PASSED] * 5
    events = (await container.events.list_events(done.run_id, limit=200)).items
    types = [e.type for e in events]
    assert types.count("WORKFLOW_NODE_STARTED") == 5 and types.count("WORKFLOW_NODE_COMPLETED") == 5
    assert "AGENT_SELECTED" in types and types[-1] == "RUN_COMPLETED"


async def test_criteria_failure_retries_then_fails_the_workflow(container: Container) -> None:
    wid = await start(container)
    for expected_attempt in (2, 3):
        run = await container.workflows.report(
            wid, node_id="understand", reporter="software_engineer", outputs={}
        )
        assert run.current_node == "understand" and run.node_runs[-1].attempt == expected_attempt
        assert run.node_runs[-2].status is NodeStatus.FAILED and "summary" in (
            run.node_runs[-2].error or ""
        )
    failed = await container.workflows.report(
        wid, node_id="understand", reporter="software_engineer", outputs={}
    )
    assert failed.status is WorkflowStatus.FAILED and "failed after 3" in (failed.error or "")
    with pytest.raises(WorkflowError, match="already failed"):
        await container.workflows.report(
            wid, node_id="understand", reporter="software_engineer", outputs=UNDERSTAND
        )
    types = [e.type for e in (await container.events.list_events(failed.run_id, limit=100)).items]
    assert "RETRY_STARTED" in types and types[-1] == "RUN_FAILED"


async def test_verification_needs_real_evidence_not_a_claim(container: Container) -> None:
    wid = await start(container)
    r = container.workflows.report
    await r(wid, node_id="understand", reporter="software_engineer", outputs=UNDERSTAND)
    await r(wid, node_id="design", reporter="software_engineer", outputs=DESIGN)
    await r(wid, node_id="implement", reporter="software_engineer", outputs=IMPLEMENT)
    # claims success but references evidence that does not exist -> not accepted
    bad = await r(
        wid,
        node_id="verify",
        reporter="code_reviewer",
        outputs={"all_passed": True},
        evidence_ids=[uuid.uuid4()],
    )
    assert bad.node_runs[-2].status is NodeStatus.FAILED
    assert "evidence" in (bad.node_runs[-2].error or "")
    # evidence belonging to another project does not count either
    other = await evidence_id(container, project_id=None)
    ok = await r(
        wid,
        node_id="verify",
        reporter="code_reviewer",
        outputs={"all_passed": True},
        evidence_ids=[other],
    )
    assert ok.current_node == "curate"


async def test_failed_verification_loops_back_to_implement_with_a_bounded_budget(
    container: Container,
) -> None:
    wid = await start(container)
    r = container.workflows.report
    await r(wid, node_id="understand", reporter="software_engineer", outputs=UNDERSTAND)
    await r(wid, node_id="design", reporter="software_engineer", outputs=DESIGN)
    await r(wid, node_id="implement", reporter="software_engineer", outputs=IMPLEMENT)
    ev = await evidence_id(container)
    nodes: list[str] = []
    for _ in range(12):
        run = await container.workflows.get(wid)
        assert run is not None
        if run.status is not WorkflowStatus.RUNNING:
            break
        node = run.current_node
        assert node is not None
        nodes.append(node)
        if node == "implement":
            await r(wid, node_id="implement", reporter="software_engineer", outputs=IMPLEMENT)
        else:
            await r(
                wid,
                node_id="verify",
                reporter="code_reviewer",
                outputs={"all_passed": False},
                evidence_ids=[ev],
            )
    final = await container.workflows.get(wid)
    assert final is not None and final.status is WorkflowStatus.FAILED  # never loops forever
    assert "verify" in nodes and "implement" in nodes
    assert final.steps <= container.workflows.definitions["engineering.default"].max_total_steps


async def test_only_the_selected_writer_may_change_files(container: Container) -> None:
    wid = await start(container, Risk.HIGH, goal="Rework token handling in the auth module")
    brief = await container.workflows.brief(wid)
    team = brief["team"]
    assert team["writer"] == "software_engineer" and "security_engineer" in team["specialists"]
    r = container.workflows.report
    await r(wid, node_id="understand", reporter="software_engineer", outputs=UNDERSTAND)
    # high risk: DESIGN is gated; approve it so we can reach IMPLEMENT
    brief = await container.workflows.brief(wid)
    await container.approvals.decide(
        uuid.UUID(brief["node"]["approval_id"]), approve=True, decided_by="human:alice"
    )
    await container.workflows.resume(wid)
    await r(wid, node_id="design", reporter="architect", outputs=DESIGN)
    with pytest.raises(WorkflowError) as exc:
        await r(wid, node_id="implement", reporter="security_engineer", outputs=IMPLEMENT)
    assert exc.value.kind == "writer_violation"
    with pytest.raises(WorkflowError) as exc2:
        await r(wid, node_id="implement", reporter="ghost_role", outputs=IMPLEMENT)
    assert exc2.value.kind == "unknown_role"
    still = await container.workflows.get(wid)
    assert (
        still is not None and still.current_node == "implement"
    )  # rejected reports change nothing
    audit = [
        e
        for e in (await container.events.list_events(still.run_id, limit=200)).items
        if e.type == "POLICY_DENIED" and e.data.get("rule") == "one_writer_many_reviewers"
    ]
    assert audit
    ok = await r(wid, node_id="implement", reporter="software_engineer", outputs=IMPLEMENT)
    assert ok.current_node == "verify"


async def test_high_risk_design_waits_for_a_human_and_cannot_be_skipped(
    container: Container,
) -> None:
    wid = await start(container, Risk.HIGH)
    r = container.workflows.report
    await r(wid, node_id="understand", reporter="software_engineer", outputs=UNDERSTAND)
    waiting = await container.workflows.get(wid)
    assert waiting is not None and waiting.status is WorkflowStatus.WAITING_APPROVAL
    assert waiting.node_runs[-1].status is NodeStatus.WAITING_APPROVAL
    with pytest.raises(WorkflowError) as exc:
        await r(wid, node_id="design", reporter="software_engineer", outputs=DESIGN)
    assert exc.value.kind == "waiting_approval"
    assert (
        await container.workflows.resume(wid)
    ).status is WorkflowStatus.WAITING_APPROVAL  # no decision yet
    approval_id = uuid.UUID((await container.workflows.brief(wid))["node"]["approval_id"])
    await container.approvals.decide(approval_id, approve=True, decided_by="human:alice")
    resumed = await container.workflows.resume(wid)
    assert (
        resumed.status is WorkflowStatus.RUNNING
        and resumed.node_runs[-1].status is NodeStatus.ACTIVE
    )
    after = await r(wid, node_id="design", reporter="software_engineer", outputs=DESIGN)
    assert after.current_node == "implement"
    low = await container.workflows.start(goal="Fix a typo in a comment", risk=Risk.LOW)
    await r(low.id, node_id="understand", reporter="software_engineer", outputs=UNDERSTAND)
    assert (await container.workflows.get(low.id)).current_node == "design"  # type: ignore[union-attr]


async def test_denied_gate_never_proceeds(container: Container) -> None:
    wid = await start(container, Risk.CRITICAL)
    await container.workflows.report(
        wid, node_id="understand", reporter="software_engineer", outputs=UNDERSTAND
    )
    approval_id = uuid.UUID((await container.workflows.brief(wid))["node"]["approval_id"])
    await container.approvals.decide(approval_id, approve=False, decided_by="human:alice")
    assert (await container.workflows.resume(wid)).status is WorkflowStatus.WAITING_APPROVAL


async def test_wrong_node_and_cancel_and_unknown_definition(container: Container) -> None:
    wid = await start(container)
    with pytest.raises(WorkflowError) as exc:
        await container.workflows.report(
            wid, node_id="implement", reporter="software_engineer", outputs=IMPLEMENT
        )
    assert exc.value.kind == "wrong_node"
    cancelled = await container.workflows.cancel(wid, actor="alice", reason="changed my mind")
    assert cancelled.status is WorkflowStatus.CANCELLED and cancelled.finished_at is not None
    with pytest.raises(WorkflowError):
        await container.workflows.cancel(wid, actor="alice", reason="again")
    with pytest.raises(WorkflowError) as exc2:
        await container.workflows.start(goal="x y z", definition_id="nope")
    assert exc2.value.kind == "unknown_definition"
    with pytest.raises(WorkflowError) as exc3:
        await container.workflows.brief(uuid.uuid4())
    assert exc3.value.kind == "not_found"


async def test_capability_criterion_uses_recorded_tool_calls(
    container: Container, tmp_path: Path
) -> None:
    from eios_domain.workflow import Criterion
    from eios_workflow.criteria import evaluate_criteria

    async with container.recorder.run(kind="t", goal="g") as ctx:
        from eios_policy import ToolInvocation

        await container.tools.invoke(ToolInvocation("sql_analyze", {"sql": "select 1"}), ctx)
    crit = [Criterion(id="c", description="used sql", kind="capability", capability="sql_analyze")]

    async def none(_: Any) -> list[Any]:
        return []

    async def seen(cap: str) -> bool:
        return await container.workflows._capability_completed(ctx.run_id, cap, EPOCH)

    ok = await evaluate_criteria(
        crit, {}, [], lookup_evidence=none, capability_completed=seen, project_id=None
    )
    assert ok[0].passed
    crit2 = [Criterion(id="c", description="used x", kind="capability", capability="test_run")]
    no = await evaluate_criteria(
        crit2, {}, [], lookup_evidence=none, capability_completed=seen, project_id=None
    )
    assert not no[0].passed


async def to_verify(c: Container, wid: uuid.UUID) -> None:
    r = c.workflows.report
    await r(wid, node_id="understand", reporter="software_engineer", outputs=UNDERSTAND)
    await r(wid, node_id="design", reporter="software_engineer", outputs=DESIGN)
    await r(wid, node_id="implement", reporter="software_engineer", outputs=IMPLEMENT)


async def events_of(c: Container, run_id: uuid.UUID) -> list[Any]:
    return (await c.events.list_events(run_id, limit=500)).items


# -- signals derived from the project ----------------------------------------------------------


async def test_start_derives_signals_from_impact_and_persists_them(
    indexed: tuple[Container, uuid.UUID],
) -> None:
    c, pid = indexed
    run = await c.workflows.start(
        goal="Rename PolicyEngine.evaluate to PolicyEngine.decide and update all callers",
        project_id=pid,
        risk=Risk.LOW,
        signals=TeamSignals(risk=Risk.LOW, symbols=("PolicyEngine.evaluate",)),
    )
    team = run.team
    assert "architect" in team.specialists
    assert "wide blast radius" in team.reasons["architect"]
    assert "security_engineer" in team.roles  # "policy" is read out of the identifier
    sig = team.signals
    assert sig["caller_risk"] == "low" and sig["dependents"] >= 8 and sig["tests_missing"]
    assert sig["explicit_targets"] and sig["symbols"] == ["PolicyEngine.evaluate"]
    assert sig["impact"]["targets"] and sig["impact"]["risk_factors"]
    assert sig["impact_error"] is None and sig["coverage"] is not None
    assert RISK_RANK[run.risk] > RISK_RANK[Risk.LOW]  # risk = max(caller, impact)
    assert sig["risk"] == run.risk.value

    stored = await c.workflows.get(run.id)
    assert stored is not None and stored.team.signals == sig
    brief = await c.workflows.brief(run.id)
    assert brief["signals"] == sig and "signals" not in brief["team"]
    classified = [e for e in await events_of(c, run.run_id) if e.type == "GOAL_CLASSIFIED"]
    assert classified[0].data["signals"]["dependents"] == sig["dependents"]


async def test_paths_are_analysed_like_symbols(indexed: tuple[Container, uuid.UUID]) -> None:
    c, pid = indexed
    run = await c.workflows.start(
        goal="Rename the evaluate method",
        project_id=pid,
        risk=Risk.LOW,
        signals=TeamSignals(risk=Risk.LOW, paths=("app/policy.py",)),
    )
    assert "architect" in run.team.specialists
    assert "wide blast radius" in run.team.reasons["architect"]
    assert run.team.signals["paths"] == ["app/policy.py"] and run.team.signals["symbols"] == []
    assert run.team.signals["impact"]["consumers"]


async def test_an_impact_failure_does_not_block_the_start(container: Container) -> None:
    run = await container.workflows.start(
        goal="Fix the retry limit",
        project_id=uuid.uuid4(),  # not a registered project
        risk=Risk.LOW,
        signals=TeamSignals(risk=Risk.LOW, paths=("shop/payments/retry.py",)),
    )
    sig = run.team.signals
    assert sig["impact"] is None and "NotFoundError" in sig["impact_error"]
    assert sig["explicit_targets"] and run.risk is Risk.LOW


async def test_documentation_paths_are_not_impact_analysed(
    indexed: tuple[Container, uuid.UUID],
) -> None:
    c, pid = indexed
    run = await c.workflows.start(
        goal="Fix a typo in the README",
        project_id=pid,
        risk=Risk.LOW,
        signals=TeamSignals(risk=Risk.LOW, paths=("README.md",)),
    )
    assert run.team.signals["impact"] is None and run.team.signals["impact_error"] is None
    assert run.risk is Risk.LOW and run.team.roles == ["technical_writer"]


# -- independent review and node roles ---------------------------------------------------------


async def test_the_writer_cannot_verify_its_own_change(container: Container) -> None:
    wid = await start(container)  # medium risk: software_engineer + code_reviewer
    await to_verify(container, wid)
    ev = await evidence_id(container)

    async def verify_as(reporter: str) -> Any:
        return await container.workflows.report(
            wid,
            node_id="verify",
            reporter=reporter,
            outputs={"all_passed": True},
            evidence_ids=[ev],
        )

    node = (await container.workflows.brief(wid))["node"]
    assert node["reporters"] == ["code_reviewer"] and node["roles"] == ["reviewer"]
    with pytest.raises(WorkflowError, match="code_reviewer") as exc:
        await verify_as("software_engineer")
    assert exc.value.kind == "self_review"
    run = await container.workflows.get(wid)
    assert run is not None and run.current_node == "verify"
    assert [n.status for n in run.node_runs if n.node_id == "verify"] == [NodeStatus.ACTIVE]
    denied = [
        e
        for e in await events_of(container, run.run_id)
        if e.type == "POLICY_DENIED" and e.data.get("rule") == "independent_review"
    ]
    assert len(denied) == 1 and denied[0].data["allowed"] == ["code_reviewer"]
    with pytest.raises(WorkflowError) as ghost:
        await verify_as("ghost_role")
    assert ghost.value.kind == "unknown_role"
    assert (await verify_as("code_reviewer")).current_node == "curate"


async def test_a_solo_writer_may_verify_when_nobody_else_is_on_the_team(
    container: Container,
) -> None:
    wid = await start(container, Risk.LOW)
    run = await container.workflows.get(wid)
    assert run is not None and run.team.roles == ["software_engineer"]
    await to_verify(container, wid)
    ev = await evidence_id(container)
    done = await container.workflows.report(
        wid,
        node_id="verify",
        reporter="software_engineer",
        outputs={"all_passed": True},
        evidence_ids=[ev],
    )
    assert done.current_node == "curate"
    waived = [
        e
        for e in await events_of(container, done.run_id)
        if e.type == "POLICY_ALLOWED" and e.data.get("waived")
    ]
    assert len(waived) == 1 and waived[0].data["rule"] == "independent_review"


async def test_curate_takes_the_knowledge_curator_or_the_writer(container: Container) -> None:
    async def at_curate() -> uuid.UUID:
        wid = await start(container)
        await to_verify(container, wid)
        await container.workflows.report(
            wid,
            node_id="verify",
            reporter="code_reviewer",
            outputs={"all_passed": True},
            evidence_ids=[await evidence_id(container)],
        )
        return wid

    wid = await at_curate()
    node = (await container.workflows.brief(wid))["node"]
    assert node["reporters"] == ["software_engineer", "knowledge_curator"]
    with pytest.raises(WorkflowError) as exc:
        await container.workflows.report(
            wid, node_id="curate", reporter="code_reviewer", outputs=CURATE
        )
    assert exc.value.kind == "role_not_allowed"  # a team member, but not for this node
    done = await container.workflows.report(
        wid, node_id="curate", reporter="knowledge_curator", outputs=CURATE
    )
    assert done.status is WorkflowStatus.COMPLETED  # the curator need not be on the team
    by_writer = await at_curate()
    assert (
        await container.workflows.report(
            by_writer, node_id="curate", reporter="software_engineer", outputs=CURATE
        )
    ).status is WorkflowStatus.COMPLETED


# -- the brief names the exact output keys -----------------------------------------------------


async def test_brief_lists_the_exact_output_keys_so_the_first_report_passes(
    container: Container,
) -> None:
    wid = await start(container)
    criteria = (await container.workflows.brief(wid))["node"]["criteria"]
    by_id = {c["id"]: c for c in criteria}
    assert set(by_id) == {"summary", "context_confidence", "unknowns"}
    for c in criteria:
        assert {"id", "description", "kind", "field", "value", "allow_empty", "min_count"} <= set(c)
        assert c["kind"] == "field" and c["field"] == c["id"]  # the id IS the output key
    assert by_id["unknowns"]["allow_empty"] is True and by_id["summary"]["allow_empty"] is False
    outputs = {c["field"]: [] if c["allow_empty"] else "x" for c in criteria}
    run = await container.workflows.report(
        wid, node_id="understand", reporter="software_engineer", outputs=outputs
    )
    assert run.node_runs[0].status is NodeStatus.PASSED  # attempt 1 was not burned
    assert run.current_node == "design" and run.node_runs[-1].attempt == 1

    r = container.workflows.report
    await r(wid, node_id="design", reporter="software_engineer", outputs=DESIGN)
    await r(wid, node_id="implement", reporter="software_engineer", outputs=IMPLEMENT)
    verify = {c["id"]: c for c in (await container.workflows.brief(wid))["node"]["criteria"]}
    assert verify["evidence"]["kind"] == "evidence" and verify["evidence"]["min_count"] == 1
    assert verify["all_passed"]["kind"] == "equals" and verify["all_passed"]["value"] is True
    assert verify["all_passed"]["field"] == "all_passed"


# -- the documentation definition --------------------------------------------------------------


async def test_a_readme_typo_completes_through_the_docs_definition_without_evidence(
    container: Container,
) -> None:
    run = await container.workflows.start(
        goal="Fix a typo in the README",
        risk=Risk.LOW,
        signals=TeamSignals(risk=Risk.LOW, paths=("README.md",)),
    )
    assert run.definition_id == "engineering.docs" and run.team.roles == ["technical_writer"]
    assert (await container.workflows.brief(run.id))["definition"] == "engineering.docs@1.0.0"
    r = container.workflows.report
    writer = "technical_writer"
    await r(run.id, node_id="understand", reporter=writer, outputs=UNDERSTAND)
    await r(
        run.id,
        node_id="implement",
        reporter=writer,
        outputs={"changed_files": ["README.md"], "notes": "fixed the typo"},
    )
    verify = (await container.workflows.brief(run.id))["node"]
    assert all(c["kind"] != "evidence" for c in verify["criteria"])
    await r(run.id, node_id="verify", reporter=writer, outputs={"all_passed": True})
    done = await r(run.id, node_id="curate", reporter=writer, outputs=CURATE)
    assert done.status is WorkflowStatus.COMPLETED
    stages = [n.stage.value for n in done.node_runs]
    assert stages == ["UNDERSTAND", "IMPLEMENT", "VERIFY", "CURATE"]


async def test_code_work_and_non_low_risk_use_the_default_definition(
    container: Container,
) -> None:
    code = await container.workflows.start(goal="Fix the retry limit", risk=Risk.LOW)
    medium = await container.workflows.start(
        goal="Fix a typo in the README",
        risk=Risk.MEDIUM,
        signals=TeamSignals(paths=("README.md",)),
    )
    explicit = await container.workflows.start(
        goal="Fix a typo in the README",
        risk=Risk.LOW,
        definition_id="engineering.default",
        signals=TeamSignals(risk=Risk.LOW, paths=("README.md",)),
    )
    got = {code.definition_id, medium.definition_id, explicit.definition_id}
    assert got == {"engineering.default"}


# -- one root span, one child span per node attempt --------------------------------------------


async def test_every_node_attempt_is_a_child_span_of_the_runs_single_root(
    container: Container,
) -> None:
    wid = await start(container)
    r = container.workflows.report
    await r(wid, node_id="understand", reporter="software_engineer", outputs={})  # fails
    await r(wid, node_id="understand", reporter="software_engineer", outputs=UNDERSTAND)
    await r(wid, node_id="design", reporter="software_engineer", outputs=DESIGN)
    run = await container.workflows.get(wid)
    assert run is not None
    graph = build_graph(run.run_id, await events_of(container, run.run_id))

    roots = [n for n in graph.nodes if n.parent_span_id is None]
    assert len(roots) == 1  # report() and friends re-attach instead of opening new roots
    root = roots[0]
    attempts = {n.label: n for n in graph.nodes if n.parent_span_id == root.span_id}
    assert {k: v.status for k, v in attempts.items()} == {
        "UNDERSTAND/understand #1": "failed",
        "UNDERSTAND/understand #2": "completed",
        "DESIGN/design #1": "completed",
        "IMPLEMENT/implement #1": "started",
    }
    assert {(e.source, e.target) for e in graph.edges} == {
        (root.span_id, a.span_id) for a in attempts.values()
    }
    assert {a.span_id for a in attempts.values()} == {n.id for n in run.node_runs}
    assert root.status == "started" and root.actor_id == "eios"

    cancelled = await container.workflows.cancel(wid, actor="alice", reason="stop")
    after = build_graph(run.run_id, await events_of(container, cancelled.run_id))
    final = {n.label: n.status for n in after.nodes}
    assert final["IMPLEMENT/implement #1"] == "failed"  # the cancelled attempt is closed
    assert final["RUN_STARTED"] == "failed"
    assert len([n for n in after.nodes if n.parent_span_id is None]) == 1


async def test_a_completed_run_is_a_connected_all_completed_graph(container: Container) -> None:
    wid = await start(container)
    await to_verify(container, wid)
    r = container.workflows.report
    await r(
        wid,
        node_id="verify",
        reporter="code_reviewer",
        outputs={"all_passed": True},
        evidence_ids=[await evidence_id(container)],
    )
    done = await r(wid, node_id="curate", reporter="software_engineer", outputs=CURATE)
    graph = build_graph(done.run_id, await events_of(container, done.run_id))
    assert len(graph.nodes) == 6 and len(graph.edges) == 5
    assert {n.status for n in graph.nodes} == {"completed"}
    assert {n.label for n in graph.nodes} >= {
        "UNDERSTAND/understand #1",
        "DESIGN/design #1",
        "IMPLEMENT/implement #1",
        "VERIFY/verify #1",
        "CURATE/curate #1",
    }


async def test_a_pending_approval_is_its_own_denied_span_under_an_open_attempt(
    container: Container,
) -> None:
    wid = await start(container, Risk.HIGH)
    await container.workflows.report(
        wid, node_id="understand", reporter="software_engineer", outputs=UNDERSTAND
    )
    run = await container.workflows.get(wid)
    assert run is not None and run.status is WorkflowStatus.WAITING_APPROVAL
    graph = build_graph(run.run_id, await events_of(container, run.run_id))
    design = next(n for n in graph.nodes if n.label == "DESIGN/design #1")
    gate = next(n for n in graph.nodes if n.parent_span_id == design.span_id)
    assert design.status == "started" and gate.status == "denied" and gate.actor_id == "policy"


async def test_workflow_api_end_to_end(client: httpx.AsyncClient) -> None:
    team = await client.post(
        "/team/select",
        json={
            "goal": "Add an index to the ledger table",
            "risk": "medium",
            "signals": {"touches_sql": True},
        },
    )
    assert team.status_code == 200 and "database_engineer" in team.json()["specialists"]
    defs = (await client.get("/workflow-definitions")).json()
    assert defs[0]["id"] == "engineering.default"
    started = await client.post("/workflows", json={"goal": "Fix the retry limit", "risk": "low"})
    assert started.status_code == 201
    wid = started.json()["workflow_id"]
    assert started.json()["node"]["id"] == "understand"
    bad = await client.post(
        f"/workflows/{wid}/report", json={"node_id": "design", "reporter": "software_engineer"}
    )
    assert bad.status_code == 409
    ok = await client.post(
        f"/workflows/{wid}/report",
        json={"node_id": "understand", "reporter": "software_engineer", "outputs": UNDERSTAND},
    )
    assert ok.status_code == 200 and ok.json()["node"]["id"] == "design"
    violation = await client.post(
        f"/workflows/{wid}/report",
        json={
            "node_id": "design",
            "reporter": "software_engineer",
            "outputs": {**DESIGN, "changed_files": ["x.py"]},
        },
    )
    assert violation.status_code == 403
    assert (await client.get(f"/workflows/{wid}")).json()["status"] == "running"
    assert (await client.get("/workflows", params={"status": "running"})).json()
    assert (await client.get(f"/workflows/{uuid.uuid4()}")).status_code == 404
    cancel = await client.post(
        f"/workflows/{wid}/cancel", json={"actor": "a", "reason": "done testing"}
    )
    assert cancel.json()["status"] == "cancelled"
    assert (
        await client.post("/workflows", json={"goal": "x y z", "definition_id": "zzz"})
    ).status_code == 404


async def test_workflow_api_enforces_independent_review_and_picks_the_docs_definition(
    client: httpx.AsyncClient,
) -> None:
    started = await client.post("/workflows", json={"goal": "Fix the retry limit"})
    assert started.status_code == 201
    wid = started.json()["workflow_id"]
    assert [c["id"] for c in started.json()["node"]["criteria"]] == [
        "summary",
        "context_confidence",
        "unknowns",
    ]
    for node_id, outputs in (
        ("understand", UNDERSTAND),
        ("design", DESIGN),
        ("implement", IMPLEMENT),
    ):
        step = await client.post(
            f"/workflows/{wid}/report",
            json={"node_id": node_id, "reporter": "software_engineer", "outputs": outputs},
        )
        assert step.status_code == 200
    self_review = await client.post(
        f"/workflows/{wid}/report",
        json={
            "node_id": "verify",
            "reporter": "software_engineer",
            "outputs": {"all_passed": True},
        },
    )
    assert self_review.status_code == 403 and "code_reviewer" in self_review.json()["detail"]

    docs = await client.post(
        "/workflows",
        json={
            "goal": "Fix a typo in the README",
            "risk": "low",
            "signals": {"paths": ["README.md"]},
        },
    )
    assert docs.json()["definition"] == "engineering.docs@1.0.0"
    team = await client.post(
        "/team/select",
        json={"goal": "Harden admin_token comparison in _require_admin", "risk": "medium"},
    )
    assert "security_engineer" in team.json()["specialists"]
