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
from eios_runtime import Container, build_container
from eios_workflow import WorkflowError
from tests.conftest import make_settings

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
        reporter="software_engineer",
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
        reporter="software_engineer",
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
        reporter="software_engineer",
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
                reporter="software_engineer",
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
