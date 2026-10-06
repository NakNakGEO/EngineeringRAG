from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from mcp.client import Client
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.policy import Risk
from eios_mcp.server import create_mcp_server
from tests.conftest import make_settings
from tests.evals.corpus import build_corpus

pytestmark = pytest.mark.integration
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
async def mcp_ctx(db: AsyncEngine, tmp_path: Path) -> Any:
    workspace = tmp_path / "workspace"
    settings = make_settings(
        workspace_roots=str(workspace), blob_dir=tmp_path / "blobs",
        manifests_dir=REPO / "manifests", workflows_dir=REPO / "workflows",
        admin_token="adm1n-secret-token",
    )  # fmt: skip
    server = create_mcp_server(settings, engine=db, readiness_checks=[])
    return server, workspace


async def call(client: Client, name: str, args: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(name, args)
    assert result.is_error is False, result
    out: Any = result.structured_content
    return dict(out)


async def test_full_session_over_mcp_context_capability_workflow_evidence(
    mcp_ctx: Any, db: AsyncEngine
) -> None:
    server, workspace = mcp_ctx
    from eios_runtime import build_container

    seed = build_container(
        make_settings(workspace_roots=str(workspace), blob_dir=workspace.parent / "b2"), db
    )
    corpus = await build_corpus(seed, db, workspace)
    pid = str(corpus.project_id)
    async with Client(server) as client:
        boot = await call(
            client, "bootstrap_project", {"path": str(workspace / "shop"), "sync": "never"}
        )
        assert boot["project_id"] == pid and boot["state"] in {"CURRENT", "current"}
        outside = await call(client, "bootstrap_project", {"path": "/etc"})
        assert outside["code"] == "outside_approved_workspace"

        ctx = await call(
            client,
            "get_context",
            {
                "text": "Before I change `PaymentGateway.charge`, what does it rely on?",
                "project_id": pid,
            },
        )
        assert ctx["ready_to_act"] is True and ctx["items"] and ctx["run_id"]
        assert all(len(i["text"]) <= 1503 for i in ctx["items"])
        found = await call(
            client, "search_knowledge", {"query": "retry policy", "project_id": pid, "limit": 5}
        )
        assert 0 < len(found["items"]) <= 5

        sql = await call(
            client,
            "request_capability",
            {
                "capability": "sql_analyze",
                "arguments": {"sql": "CREATE TABLE t (id int);"},
                "run_id": ctx["run_id"],
            },
        )
        assert sql["status"] == "ok" and sql["provider"] == "sql_analyze"
        gap = await call(client, "request_capability", {"capability": "file_edit", "arguments": {}})
        assert gap["status"] == "no_provider" and "Do not improvise" in gap["next"]
        evil = await call(
            client, "request_capability", {"capability": "external_database_read", "arguments": {}}
        )
        assert evil["status"] == "denied" and evil["output"] is None
        guess = await call(
            client, "request_capability", {"capability": "run_shell", "arguments": {"cmd": "ls"}}
        )
        assert guess["status"] == "no_provider" and guess["output"] is None

        spec = await call(
            client,
            "request_specialist",
            {"goal": "Add an index to the ledger table", "touches_data_layer": True},
        )
        roles = [d["role"] for d in spec["definitions"]]
        assert "database_engineer" in roles and spec["team"]["writer"] == "software_engineer"
        named = await call(client, "request_specialist", {"goal": "x y z", "role": "architect"})
        assert named["definitions"][0]["can_write"] is False
        assert "error" in await call(
            client, "request_specialist", {"goal": "x y z", "role": "nonexistent"}
        )

        # workflow driven through MCP only
        from eios_runtime import Container

        container: Container = build_container(
            make_settings(
                workspace_roots=str(workspace),
                blob_dir=workspace.parent / "b3",
                manifests_dir=REPO / "manifests",
                workflows_dir=REPO / "workflows",
            ),
            db,
        )
        wf = await container.workflows.start(
            goal="Fix the retry limit in the token auth module",
            project_id=corpus.project_id,
            risk=Risk.LOW,
        )
        wid = str(wf.id)
        rep = await call(
            client,
            "report_result",
            {
                "workflow_id": wid,
                "node_id": "understand",
                "reporter": "software_engineer",
                "outputs": {"summary": "s", "context_confidence": 0.9, "unknowns": []},
            },
        )
        assert rep["node"]["id"] == "design"
        bad = await call(
            client,
            "report_result",
            {
                "workflow_id": wid,
                "node_id": "design",
                "reporter": "security_engineer",
                "outputs": {"plan": "p", "changed_files": ["a.py"]},
            },
        )
        assert bad["code"] == "writer_violation"
        await call(
            client,
            "report_result",
            {
                "workflow_id": wid,
                "node_id": "design",
                "reporter": "software_engineer",
                "outputs": {"plan": "p", "reuse_candidates": []},
            },
        )
        await call(
            client,
            "report_result",
            {
                "workflow_id": wid,
                "node_id": "implement",
                "reporter": "software_engineer",
                "outputs": {"changed_files": ["shop/payments/retry.py"], "notes": "n"},
            },
        )
        claim = await call(
            client,
            "report_result",
            {
                "workflow_id": wid,
                "node_id": "verify",
                "reporter": "software_engineer",
                "outputs": {"all_passed": True},
            },
        )
        assert (
            claim["node"]["id"] == "verify" and claim["node"]["last_failure"]
        )  # claim alone fails
        proof = await call(
            client,
            "report_result",
            {
                "workflow_id": wid,
                "node_id": "verify",
                "reporter": "software_engineer",
                "outputs": {"all_passed": True},
                "evidence": [
                    {"content": "3 passed", "summary": "pytest output", "label": "pytest"}
                ],
            },
        )
        assert proof["node"]["id"] == "curate"

        state = await call(client, "get_run_state", {"workflow_id": wid})
        assert state["workflow"]["status"] == "running" and state["run"]["kind"] == "workflow"
        assert state["events"] and "seq" in state["events"][0]
        assert "error" in await call(client, "get_run_state", {})
        assert "error" in await call(client, "get_run_state", {"run_id": "not-a-uuid"})


async def test_inline_evidence_is_attributed_to_the_llm_not_to_a_tool(
    mcp_ctx: Any, db: AsyncEngine
) -> None:
    server, workspace = mcp_ctx
    from eios_runtime import build_container

    c = build_container(
        make_settings(
            workspace_roots=str(workspace),
            blob_dir=workspace.parent / "b4",
            manifests_dir=REPO / "manifests",
            workflows_dir=REPO / "workflows",
        ),
        db,
    )
    await c.sync_registries()
    wf = await c.workflows.start(goal="Fix the retry limit", risk=Risk.LOW)
    async with Client(server) as client:
        for node, outs in (
            ("understand", {"summary": "s", "context_confidence": 1, "unknowns": []}),
            ("design", {"plan": "p", "reuse_candidates": []}),
            ("implement", {"changed_files": [], "notes": "n"}),
        ):
            await call(
                client,
                "report_result",
                {
                    "workflow_id": str(wf.id),
                    "node_id": node,
                    "reporter": "software_engineer",
                    "outputs": outs,
                },
            )
        too_many = await call(
            client,
            "report_result",
            {
                "workflow_id": str(wf.id),
                "node_id": "verify",
                "reporter": "software_engineer",
                "outputs": {},
                "evidence": [{"content": "x"}] * 11,
            },
        )
        assert "at most" in too_many["error"]
    brief = await c.workflows.get(wf.id)
    assert brief is not None and brief.current_node == "verify"


async def test_get_evidence_returns_metadata_and_bounded_content(
    mcp_ctx: Any, db: AsyncEngine
) -> None:
    server, workspace = mcp_ctx
    from eios_runtime import build_container

    c = build_container(
        make_settings(workspace_roots=str(workspace), blob_dir=workspace.parent / "blobs"), db
    )
    record = await c.pipeline.ingest_raw(
        ("line\n" * 5000).encode(), tool_id="pytest", summary="big output"
    )
    async with Client(server) as client:
        out = await call(client, "get_evidence", {"evidence_id": str(record.id), "max_chars": 200})
        assert out["tool_id"] == "pytest" and out["trust"] == "RAW"
        assert len(out["content"]) == 200 and out["truncated"] is True
        assert (await call(client, "get_evidence", {"evidence_id": str(uuid.uuid4())}))[
            "code"
        ] == "not_found"
        assert "error" in await call(client, "get_evidence", {"evidence_id": "nope"})
    assert json.dumps(out)  # JSON-serialisable
