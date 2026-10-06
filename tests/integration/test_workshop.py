from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_capability import RoutingRequest, RoutingStatus
from eios_domain.knowledge import HumanApproval
from eios_domain.registry import RegistryState, TransitionError
from eios_policy import ToolInvocation
from eios_runtime import Container, build_container
from eios_workshop import (
    GapRequest,
    GapStatus,
    ProposalKind,
    ProposalState,
    WorkshopError,
)
from tests.conftest import make_settings

pytestmark = pytest.mark.integration
REPO = Path(__file__).resolve().parents[2]
HUMAN = HumanApproval(approver="human:alice", scope="workshop")

TOOL_SOURCE = textwrap.dedent(
    """
    import json
    import sys

    data = json.load(sys.stdin)
    text = str(data.get("text", ""))
    print(json.dumps({"upper": text.upper(), "length": len(text)}))
    """
)
TOOL_TESTS = [
    {"id": "basic", "arguments": {"text": "abc"}, "expect": {"upper": "ABC", "length": 3}},
    {"id": "empty", "arguments": {"text": ""}, "expect": {"upper": "", "length": 0}},
    {"id": "unicode", "arguments": {"text": "straße"}, "expect": {"length": 6}},
]


def tool_spec(name: str = "text_upper", version: str = "1.0.0", **over: Any) -> dict[str, Any]:
    spec = {
        "id": name,
        "version": version,
        "description": "Upper-cases text for tests.",
        "capabilities": ["text_transform"],
    }
    spec.update(over)
    return spec


NEW_CAP = [
    {
        "id": "text_transform",
        "description": "Transform plain text deterministically.",
        "risk": "low",
    }
]


@pytest.fixture
async def c(db: AsyncEngine, tmp_path: Path) -> Container:
    c = build_container(
        make_settings(
            manifests_dir=REPO / "manifests",
            workflows_dir=REPO / "workflows",
            evals_dir=REPO / "evals",
            blob_dir=tmp_path / "b",
            workshop_dir=tmp_path / "workshop",
            sandbox_require_network_isolation=False,
            sandbox_output_dir=tmp_path / "sbx",
        ),
        db,
    )
    assert (await c.sync_registries()).ok
    return c


# ---- gap resolver ----
async def resolve(c: Container, **kw: Any) -> Any:
    return await c.resolver.resolve(GapRequest(**kw))


async def test_resolution_order_and_statuses(c: Container) -> None:
    r = await resolve(c, capability="sql_analyze")
    assert r.status is GapStatus.RESOLVED and r.provider["id"] == "sql_analyze"
    r = await resolve(c, description="analyze sql text and find the tables it references")
    assert r.status is GapStatus.RESOLVED and r.capability == "sql_analyze"
    r = await resolve(
        c,
        description="check impact then inspect git history",
        capability="change_review",
        components=["impact_analyze", "git_inspect"],
    )
    assert r.status is GapStatus.COMPOSABLE and r.plan == ["impact_analyze", "git_inspect"]
    r = await resolve(c, capability="file_edit", description="edit files in the workspace")
    assert (
        r.status is GapStatus.GENERATABLE and r.artifact_kind is ProposalKind.TOOL and r.needs_human
    )
    r = await resolve(c, capability="pdf_extract", description="extract text from a PDF binary")
    assert r.status is GapStatus.GENERATABLE and r.artifact_kind is ProposalKind.TOOL
    r = await resolve(
        c, capability="release_notes", description="write release notes procedure from commits"
    )
    assert (
        r.status is GapStatus.GENERATABLE
        and r.artifact_kind is ProposalKind.SKILL
        and not r.needs_human
    )
    r = await resolve(
        c, capability="threat_reviewer", description="act as a threat modelling specialist"
    )
    assert r.status is GapStatus.GENERATABLE and r.artifact_kind is ProposalKind.AGENT
    r = await resolve(
        c, capability="search_cluster", description="a long-running search cluster service"
    )
    assert r.status is GapStatus.EXTERNAL_REQUIRED and r.needs_human
    assert (await resolve(c)).status is GapStatus.IMPOSSIBLE
    r = await resolve(
        c, capability="kernel_probe", description="inspect hardware", requires=["kernel"]
    )
    assert r.status is GapStatus.IMPOSSIBLE


@pytest.mark.parametrize(
    ("capability", "description"),
    [
        ("external_database_read", "read orders"),
        ("external_database_execute", ""),
        ("root_policy_editor", "edit the policy"),
        (None, "connect to the production database and read orders"),
        ("data_sync", "execute sql against the company postgres every night"),
        ("fancy", "reuse"),
    ],
)
async def test_database_and_policy_needs_are_blocked_before_any_generation(
    c: Container, capability: str | None, description: str
) -> None:
    r = await resolve(c, capability=capability, description=description)
    if capability == "fancy":
        assert (
            r.status is not GapStatus.BLOCKED_BY_POLICY
        )  # control: ordinary needs are not blocked
        return
    assert r.status is GapStatus.BLOCKED_BY_POLICY
    assert not r.artifact_kind and "never provided" in r.message
    assert await c.workshop.list_proposals() == []
    denied = await c.audit.list(effect="deny")
    assert any(a.action == "workshop.generate" for a in denied) or capability in {
        "root_policy_editor"
    }


async def test_composition_is_conservative(c: Container) -> None:
    broken = await resolve(
        c, capability="combo", description="x y z", components=["sql_analyze", "file_edit"]
    )
    assert broken.status is not GapStatus.COMPOSABLE  # file_edit has no provider


# ---- skills & agents ----
def skill_spec(name: str = "release-notes", caps: list[str] | None = None) -> dict[str, Any]:
    caps = caps or ["git_inspect"]
    return {
        "id": name,
        "version": "1.0.0",
        "goal": "Draft release notes from the commit history.",
        "trigger": "a release is being prepared",
        "required_capabilities": caps,
        "procedure": [
            {
                "id": "log",
                "instruction": "Read the commits since the last tag.",
                "capability": caps[0],
            }
        ],
        "completion_criteria": ["Notes list every user-visible change"],
    }


async def test_generated_skill_is_tested_automatically_and_registers_experimental(
    c: Container,
) -> None:
    p = await c.workshop.create(
        kind=ProposalKind.SKILL,
        spec=skill_spec(),
        creator="gpt-x",
        prompt="write a skill",
        need={"capability": "release_notes"},
    )
    assert p.state is ProposalState.DRAFT and p.prompt_hash and p.creator == "gpt-x"
    p = await c.workshop.sandbox(p.id, actor="a")
    p = await c.workshop.test(p.id, actor="a")
    assert p.state is ProposalState.TESTED and p.test_results and p.test_results["passed"]
    p = await c.workshop.register(p.id, actor="a")  # no human needed for a skill
    assert p.state is ProposalState.EXPERIMENTAL and p.registered_ref == "release-notes@1.0.0"
    row = await c.registry.store.get_skill("release-notes")
    assert row is not None and row["state"] == "EXPERIMENTAL" and row["origin"] == "generated"
    with pytest.raises(WorkshopError, match="already exists"):
        await c.workshop.create(kind=ProposalKind.SKILL, spec=skill_spec(), creator="gpt-x")
    history = await c.workshop.history(p.id)
    assert [h["to_state"] for h in history] == ["DRAFT", "SANDBOXED", "TESTED", "EXPERIMENTAL"]


async def test_skill_with_unregistered_capability_fails_its_tests(c: Container) -> None:
    p = await c.workshop.create(
        kind=ProposalKind.SKILL, spec=skill_spec("bad-skill", ["not_a_capability"]), creator="m"
    )
    await c.workshop.sandbox(p.id, actor="a")
    p = await c.workshop.test(p.id, actor="a")
    assert p.state is ProposalState.SANDBOXED and p.test_results and not p.test_results["passed"]
    with pytest.raises(WorkshopError, match="TESTED"):
        await c.workshop.register(p.id, actor="a")


async def test_generated_agent_is_read_only_inherits_root_policy_and_starts_experimental(
    c: Container,
) -> None:
    spec = {
        "id": "threat_reviewer",
        "version": "1.0.0",
        "role": "Threat Reviewer",
        "description": "Reviews designs for threats.",
        "prompt": "You review designs for threats and report findings.",
        "capabilities": ["source_read"],
        "forbidden_actions": ["send email"],
    }
    with pytest.raises(WorkshopError, match="writers"):
        await c.workshop.create(
            kind=ProposalKind.AGENT,
            spec={**spec, "can_write": True, "permissions": {"write": True}},
            creator="m",
        )
    p = await c.workshop.create(kind=ProposalKind.AGENT, spec=spec, creator="m")
    for step in (c.workshop.sandbox, c.workshop.test, c.workshop.register):
        p = await step(p.id, actor="a")
    agent = await c.registry.store.get_agent("threat_reviewer")
    assert agent is not None and agent["state"] == "EXPERIMENTAL" and not agent["can_write"]
    forbidden = agent["manifest"]["forbidden_actions"]
    assert "send email" in forbidden and "bypass or modify Root Policy" in forbidden
    assert any("external database" in f for f in forbidden)


# ---- tools ----
async def make_tool(c: Container, **over: Any) -> Any:
    return await c.workshop.create(
        kind=ProposalKind.TOOL,
        spec=tool_spec(**over),
        source=TOOL_SOURCE,
        tests=TOOL_TESTS,
        new_capabilities=NEW_CAP,
        creator="gpt-x",
        prompt="make a tool",
    )


async def test_generated_tool_full_lifecycle(c: Container) -> None:
    p = await make_tool(c)
    p = await c.workshop.sandbox(p.id, actor="a")
    assert p.scan_report and p.scan_report["ok"] and p.source_sha256 and p.artifact_path
    p = await c.workshop.test(p.id, actor="a")
    assert p.state is ProposalState.TESTED and len(p.test_results["results"]) == 3  # type: ignore[index]
    with pytest.raises(WorkshopError) as exc:
        await c.workshop.register(p.id, actor="a")  # no human
    assert exc.value.kind == "approval_required"
    # still not routable
    assert (await c.router.resolve(RoutingRequest(capability="text_transform"))).status in {
        RoutingStatus.UNKNOWN_CAPABILITY,
        RoutingStatus.NO_PROVIDER,
    }
    p = await c.workshop.register(p.id, actor="a", human=HUMAN)
    assert (
        p.state is ProposalState.EXPERIMENTAL
        and p.approval
        and p.approval["approver"] == "human:alice"
    )
    routed = await c.router.resolve(RoutingRequest(capability="text_transform"))
    assert routed.status is RoutingStatus.SELECTED and routed.selected
    result = await c.tools.invoke(ToolInvocation("text_transform", {"text": "hello"}))
    assert result.status == "ok" and result.output == {"upper": "HELLO", "length": 5}, result
    row = await c.registry.store.get_provider("text_upper")
    assert row is not None and row.origin.value == "generated" and row.approved_by == "human:alice"
    p = await c.workshop.verify(p.id, human=HUMAN)
    assert p.state is ProposalState.VERIFIED
    p = await c.workshop.trust(p.id, human=HUMAN)
    assert p.state is ProposalState.TRUSTED
    final = await c.registry.store.get_provider("text_upper")
    assert final is not None and final.state is RegistryState.TRUSTED
    # tampering with the artifact after approval is caught by the pinned hash
    Path(p.artifact_path or "").write_text('#!/bin/sh\necho \'{"upper": "EVIL"}\'\n')
    tampered = await c.tools.invoke(ToolInvocation("text_transform", {"text": "x"}))
    assert tampered.status == "denied" and "SHA-256" in (tampered.error or "")


async def test_malicious_tool_sources_never_reach_the_sandbox(c: Container) -> None:
    for i, source in enumerate(
        [
            "import socket\ns = socket.socket()\n",
            "import psycopg\npsycopg.connect('x')\n",
            "import os\nos.system('psql')\n",
            "exec(open('x').read())\n",
            "print(open('policy/root_policy.yaml').read())\n",
        ]
    ):
        p = await c.workshop.create(
            kind=ProposalKind.TOOL,
            spec=tool_spec(f"evil_{i}"),
            source=source,
            tests=TOOL_TESTS,
            new_capabilities=NEW_CAP,
            creator="m",
        )
        with pytest.raises(WorkshopError) as exc:
            await c.workshop.sandbox(p.id, actor="a")
        assert exc.value.kind == "scan_failed"
        stored = await c.workshop.get(p.id)
        assert stored.state is ProposalState.DRAFT and stored.artifact_path is None
        assert stored.scan_report and not stored.scan_report["ok"]


async def test_tool_proposals_cannot_ask_for_privileges_or_forbidden_capabilities(
    c: Container,
) -> None:
    for over, expected in (
        (
            {
                "permissions": {"network": "allowlist", "network_hosts": ["x.org"]},
                "network_required": True,
            },
            "no network",
        ),
        (
            {"permissions": {"filesystem": [{"scope": "approved_workspace", "mode": "write"}]}},
            "no network",
        ),
        ({"permissions": {"secrets": ["API_KEY"]}}, "no network"),
    ):
        with pytest.raises(WorkshopError, match=expected):
            await c.workshop.create(
                kind=ProposalKind.TOOL,
                spec=tool_spec(**over),
                source=TOOL_SOURCE,
                tests=TOOL_TESTS,
                creator="m",
            )
    for caps in (["external_database_read"], ["root_policy_write"], ["policy_modify_rules"]):
        with pytest.raises(WorkshopError) as exc:
            await c.workshop.create(
                kind=ProposalKind.TOOL,
                spec=tool_spec("sneaky", capabilities=caps),
                source=TOOL_SOURCE,
                tests=TOOL_TESTS,
                creator="m",
            )
        assert exc.value.kind == "blocked_by_policy"
    with pytest.raises(WorkshopError) as exc:
        await c.workshop.create(
            kind=ProposalKind.TOOL,
            spec=tool_spec("db_helper"),
            source=TOOL_SOURCE,
            tests=TOOL_TESTS,
            need={"description": "connect to the production database"},
            new_capabilities=NEW_CAP,
            creator="m",
        )
    assert exc.value.kind == "blocked_by_policy"
    with pytest.raises(WorkshopError, match="source"):
        await c.workshop.create(kind=ProposalKind.TOOL, spec=tool_spec("nosrc"), creator="m")
    assert await c.workshop.list_proposals() == []  # nothing was stored


async def test_failing_tool_tests_keep_it_sandboxed_and_unregistered(c: Container) -> None:
    tests = [{"id": "wrong", "arguments": {"text": "a"}, "expect": {"upper": "NOPE"}}]
    p = await c.workshop.create(
        kind=ProposalKind.TOOL,
        spec=tool_spec("flaky_tool"),
        source=TOOL_SOURCE,
        tests=tests,
        new_capabilities=NEW_CAP,
        creator="m",
    )
    await c.workshop.sandbox(p.id, actor="a")
    p = await c.workshop.test(p.id, actor="a")
    assert p.state is ProposalState.SANDBOXED and not p.test_results["passed"]  # type: ignore[index]
    with pytest.raises(WorkshopError, match="TESTED"):
        await c.workshop.register(p.id, actor="a", human=HUMAN)
    assert await c.registry.store.get_provider("flaky_tool") is None
    crash = await c.workshop.create(
        kind=ProposalKind.TOOL,
        spec=tool_spec("crashy"),
        source="raise SystemExit(3)\n",
        tests=[{"id": "t", "arguments": {}, "expect": {"x": 1}}],
        new_capabilities=NEW_CAP,
        creator="m",
    )
    await c.workshop.sandbox(crash.id, actor="a")
    out = await c.workshop.test(crash.id, actor="a")
    assert out.test_results and "exit 3" in out.test_results["results"][0]["detail"]


async def test_verification_needs_enough_tests_and_a_human_and_trust_needs_verification(
    c: Container,
) -> None:
    p = await c.workshop.create(
        kind=ProposalKind.TOOL,
        spec=tool_spec("thin_tool"),
        source=TOOL_SOURCE,
        tests=TOOL_TESTS[:1],
        new_capabilities=NEW_CAP,
        creator="m",
    )
    for step in (c.workshop.sandbox, c.workshop.test):
        p = await step(p.id, actor="a")
    p = await c.workshop.register(p.id, actor="a", human=HUMAN)
    with pytest.raises(WorkshopError, match="at least 3"):
        await c.workshop.verify(p.id, human=HUMAN)
    with pytest.raises(WorkshopError, match="VERIFIED"):
        await c.workshop.trust(p.id, human=HUMAN)
    with pytest.raises(TransitionError):  # the registry also refuses TRUSTED without a human
        await c.registry.transition(
            "provider", "thin_tool", "1.0.0", RegistryState.TRUSTED, actor="bot", reason="x"
        )


async def test_reject_disables_a_registered_artifact(c: Container) -> None:
    p = await make_tool(c, name="rejectable") if False else await make_tool(c, id="rejectable")
    for step in (c.workshop.sandbox, c.workshop.test):
        p = await step(p.id, actor="a")
    p = await c.workshop.register(p.id, actor="a", human=HUMAN)
    with pytest.raises(WorkshopError, match="reason"):
        await c.workshop.reject(p.id, actor="alice", reason=" ")
    out = await c.workshop.reject(p.id, actor="alice", reason="found a problem")
    assert out.state is ProposalState.REJECTED
    row = await c.registry.store.get_provider("rejectable")
    assert row is not None and row.state is RegistryState.DISABLED
    assert (
        await c.router.resolve(RoutingRequest(capability="text_transform"))
    ).status is RoutingStatus.NO_PROVIDER


# ---- API ----
async def test_workshop_api_enforces_human_gates(client: httpx.AsyncClient) -> None:
    gap = await client.post("/gaps/resolve", json={"capability": "external_database_read"})
    assert gap.json()["status"] == "BLOCKED_BY_POLICY"
    gap = await client.post(
        "/gaps/resolve",
        json={"capability": "release_notes", "description": "write release notes procedure"},
    )
    assert gap.json()["status"] == "GENERATABLE" and gap.json()["artifact_kind"] == "skill"

    body = {"kind": "skill", "spec": skill_spec("api-skill"), "creator": "some-model"}
    created = await client.post("/workshop/proposals", json=body)
    assert created.status_code == 201
    pid = created.json()["id"]
    assert (await client.post("/workshop/proposals", json=body)).status_code == 409
    assert (
        await client.post("/workshop/proposals", json={**body, "kind": "policy"})
    ).status_code == 422
    forbidden = await client.post(
        "/workshop/proposals",
        json={
            "kind": "skill",
            "creator": "m",
            "spec": skill_spec("sneaky-skill", ["external_database_execute"]),
        },
    )
    assert forbidden.status_code == 403
    assert (
        await client.post(f"/workshop/proposals/{pid}/test")
    ).status_code == 409  # not sandboxed yet
    assert (await client.post(f"/workshop/proposals/{pid}/sandbox")).status_code == 200
    assert (await client.post(f"/workshop/proposals/{pid}/test")).json()["state"] == "TESTED"
    reg = await client.post(f"/workshop/proposals/{pid}/register", json={})
    assert reg.status_code == 200 and reg.json()["state"] == "EXPERIMENTAL"
    got = (await client.get(f"/workshop/proposals/{pid}")).json()
    assert [h["to"] for h in got["history"]] == ["DRAFT", "SANDBOXED", "TESTED", "EXPERIMENTAL"]
    assert (await client.get("/workshop/proposals", params={"state": "EXPERIMENTAL"})).json()[0][
        "id"
    ] == pid
    # humans only for verify / trust
    assert (
        await client.post(f"/workshop/proposals/{pid}/verify", json={"approver": "alice"})
    ).status_code in {401, 403}
    assert (await client.post(f"/workshop/proposals/{pid}/trust", json={})).status_code in {
        401,
        403,
    }
    rejected = await client.post(
        f"/workshop/proposals/{pid}/reject", json={"reason": "obsolete", "approver": "alice"}
    )
    assert rejected.json()["state"] == "REJECTED"
    assert (
        await client.get("/workshop/proposals/00000000-0000-0000-0000-000000000000")
    ).status_code == 404
