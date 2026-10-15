from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_capability import RoutingRequest, load_directory
from eios_domain.events import ActorType
from eios_domain.policy import PolicyEffect, PolicyRequest
from eios_domain.registry import Origin, RegistryState
from eios_policy import ApprovalError, ToolInvocation
from eios_runtime import Container, build_container
from tests.conftest import make_settings

pytestmark = pytest.mark.integration
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
async def container(db: AsyncEngine, tmp_path: Path) -> Container:
    c = build_container(
        make_settings(
            manifests_dir=REPO / "manifests",
            blob_dir=tmp_path / "blobs",
            sandbox_require_network_isolation=False,
            sandbox_output_dir=tmp_path / "sbx",
        ),
        db,
    )
    assert (await c.sync_registries()).ok
    return c


def push_request(target: str = "origin/main") -> PolicyRequest:
    return PolicyRequest(
        actor_type=ActorType.AGENT, actor_id="software_engineer", action="git.push", target=target
    )


async def test_approval_lifecycle_is_single_use_and_bound_to_the_action(
    container: Container,
) -> None:
    first = await container.policy.evaluate(push_request())
    assert first.effect is PolicyEffect.REQUIRE_APPROVAL and first.approval_id
    again = await container.policy.evaluate(push_request())
    assert again.approval_id == first.approval_id  # one pending request, not a flood
    # an LLM-side party cannot self-approve: only decide() with a named human can
    with pytest.raises(ApprovalError):
        await container.approvals.decide(first.approval_id, approve=True, decided_by=" ")
    await container.approvals.decide(first.approval_id, approve=True, decided_by="human:alice")
    # approval for origin/main is useless for another target or another actor
    other = await container.policy.evaluate(push_request("origin/release"))
    assert other.effect is PolicyEffect.REQUIRE_APPROVAL
    ok = await container.policy.evaluate(push_request())
    assert ok.effect is PolicyEffect.ALLOW and ok.rule_id == "approval.consumed"
    reused = await container.policy.evaluate(push_request())
    assert reused.effect is PolicyEffect.REQUIRE_APPROVAL  # single use
    with pytest.raises(ApprovalError):  # decided approvals cannot be re-decided
        await container.approvals.decide(first.approval_id, approve=False, decided_by="human:bob")


async def test_denied_and_expired_approvals_never_authorise(container: Container) -> None:
    d = await container.policy.evaluate(push_request("origin/a"))
    assert d.approval_id
    await container.approvals.decide(d.approval_id, approve=False, decided_by="human:alice")
    assert (
        await container.policy.evaluate(push_request("origin/a"))
    ).effect is PolicyEffect.REQUIRE_APPROVAL
    e = await container.policy.evaluate(push_request("origin/b"))
    assert e.approval_id
    await container.approvals.decide(e.approval_id, approve=True, decided_by="human:alice")
    async with container.engine.begin() as conn:
        await conn.execute(
            sa.text(
                "UPDATE policy.approval SET expires_at = now() - interval '1 hour' WHERE id = :i"
            ),
            {"i": e.approval_id},
        )
    assert (
        await container.policy.evaluate(push_request("origin/b"))
    ).effect is PolicyEffect.REQUIRE_APPROVAL


async def test_audit_log_records_decisions_and_is_append_only(container: Container) -> None:
    await container.policy.evaluate(
        PolicyRequest(actor_type=ActorType.LLM, actor_id="x", action="db.read", target="prod")
    )
    entries = await container.audit.list(effect="deny")
    assert entries and entries[0].rule_id == "root.external_database"
    async with container.engine.begin() as conn:
        with pytest.raises(DBAPIError, match="append-only"):
            await conn.execute(sa.text("UPDATE policy.audit_log SET effect = 'allow'"))
    async with container.engine.begin() as conn:
        with pytest.raises(DBAPIError, match="append-only"):
            await conn.execute(sa.text("DELETE FROM policy.audit_log"))


async def test_runtime_runs_a_registered_tool_and_records_everything(container: Container) -> None:
    async with container.recorder.run(kind="tool", goal="parse sql") as ctx:
        result = await container.tools.invoke(
            ToolInvocation("sql_analyze", {"sql": "CREATE TABLE t (id int);\nSELECT * FROM t;"}),
            ctx,
        )
    assert result.status == "ok" and result.provider == "sql_analyze"
    assert result.output and "objects" in result.output
    types = [e.type for e in (await container.events.list_events(ctx.run_id, limit=200)).items]
    for expected in (
        "CAPABILITY_REQUESTED",
        "TOOL_SELECTED",
        "POLICY_ALLOWED",
        "TOOL_STARTED",
        "TOOL_COMPLETED",
    ):
        assert expected in types
    metrics = await container.registry.store.metrics_for("sql_analyze")
    assert metrics[("sql_analyze", "1.0.0")].successes == 1


async def test_runtime_refuses_forbidden_unknown_and_missing_capabilities(
    container: Container,
) -> None:
    for cap, status in (
        ("external_database_read", "denied"),
        ("teleport", "no_provider"),
        ("file_edit", "no_provider"),
    ):
        result = await container.tools.invoke(ToolInvocation(cap, {"sql": "select 1"}))
        assert result.status == status and result.output is None
    audit = await container.audit.list(effect="deny")
    assert any(a.rule_id == "root.forbidden_capability" for a in audit)


async def test_failing_tool_is_reported_not_raised_and_counts_against_the_provider(
    container: Container,
) -> None:
    result = await container.tools.invoke(
        ToolInvocation("source_read", {"project_id": "not-a-uuid", "path": "x"})
    )
    assert result.status == "failed" and result.error
    m = await container.registry.store.metrics_for("source_read")
    assert m[("source_read", "1.0.0")].failures == 1


async def test_generated_tool_cannot_run_until_approved_then_runs_in_the_sandbox(
    container: Container, tmp_path: Path
) -> None:
    exe = tmp_path / "gen_tool.sh"
    exe.write_text("#!/bin/sh\ncat >/dev/null\necho '{\"answer\": 42}'\n")
    exe.chmod(0o755)
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "t.yaml").write_text(
        f"kind: tool\nid: gen_tool\nversion: 1.0.0\ntype: subprocess\n"
        f"description: Generated subprocess tool for tests.\ncapabilities: [lint_run]\n"
        f"entrypoint: subprocess:{exe}\nsha256: {digest}\n"
    )
    await container.registry.sync(load_directory(tmp_path / "m"), origin=Origin.GENERATED)
    await container.health.run_all()
    call = ToolInvocation(
        "lint_run", {"x": 1}, routing=RoutingRequest("lint_run", allow_experimental=True)
    )
    assert (await container.tools.invoke(call)).status == "no_provider"  # awaiting human approval
    await container.registry.transition(
        "provider",
        "gen_tool",
        "1.0.0",
        RegistryState.EXPERIMENTAL,
        actor="owner",
        reason="reviewed code",
        human_approved=True,
    )
    await container.health.run_all()
    result = await container.tools.invoke(call)
    assert result.status == "ok" and result.output == {"answer": 42}, result
    # swap the binary after registration: the pinned hash catches it
    exe.write_text('#!/bin/sh\necho \'{"answer": "evil"}\'\n')
    exe.chmod(0o755)
    tampered = await container.tools.invoke(call)
    assert tampered.status == "denied" and "SHA-256" in (tampered.error or "")


async def test_approval_api_requires_the_admin_token(
    db: AsyncEngine, migrated_database_url: str, tmp_path: Path
) -> None:
    from httpx import ASGITransport

    from eios_api.app import create_app

    settings = make_settings(
        database_url=migrated_database_url,
        blob_dir=tmp_path / "b",
        admin_token="adm1n-secret-token",
    )
    app = create_app(settings, engine=db, readiness_checks=[])
    container: Container = app.state.container
    decision = await container.policy.evaluate(push_request("origin/api"))
    url = f"/approvals/{decision.approval_id}/decision"
    body = {"approve": True, "decided_by": "alice"}
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        assert (await http.post(url, json=body)).status_code == 401
        assert (
            await http.post(url, json=body, headers={"X-EIOS-Admin-Token": "wrong"})
        ).status_code == 401
        pending = (await http.get("/approvals", params={"status": "pending"})).json()
        assert [a["id"] for a in pending] == [str(decision.approval_id)]
        ok = await http.post(url, json=body, headers={"X-EIOS-Admin-Token": "adm1n-secret-token"})
        assert ok.status_code == 200 and ok.json()["decided_by"] == "human:alice"
        assert (
            await http.post(url, json=body, headers={"X-EIOS-Admin-Token": "adm1n-secret-token"})
        ).status_code == 409
        assert (await http.get(f"/approvals/{uuid.uuid4()}")).status_code == 404
        root = (await http.get("/policy/root")).json()
        assert (
            root["mutable_at_runtime"] is False
            and "external_database_read" in root["forbidden_capabilities"]
        )
        dry = (
            await http.post("/policy/evaluate", json={"action": "db.read", "target": "x"})
        ).json()
        assert dry["effect"] == "deny" and dry["rule_id"] == "root.external_database"
        audit = (await http.get("/audit", params={"effect": "require_approval"})).json()
        assert audit


async def test_approvals_are_disabled_without_an_admin_token(
    db: AsyncEngine, migrated_database_url: str, tmp_path: Path
) -> None:
    from httpx import ASGITransport

    from eios_api.app import create_app

    app = create_app(
        make_settings(database_url=migrated_database_url, blob_dir=tmp_path / "b"),
        engine=db,
        readiness_checks=[],
    )
    d = await app.state.container.policy.evaluate(push_request("origin/x"))
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        r = await http.post(
            f"/approvals/{d.approval_id}/decision",
            json={"approve": True, "decided_by": "a"},
            headers={"X-EIOS-Admin-Token": "anything"},
        )
        assert r.status_code == 403


def test_blank_or_short_admin_tokens_never_enable_approvals() -> None:
    assert make_settings(admin_token="").admin_token is None
    assert make_settings(admin_token="   ").admin_token is None
    with pytest.raises(ValueError, match="at least 12"):
        make_settings(admin_token="short")
