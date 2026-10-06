"""Adversarial tests for the Policy Engine and Root Policy (no database needed)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from eios_domain.events import ActorType
from eios_domain.policy import PolicyEffect, PolicyRequest
from eios_policy import NoApprovals, NullAudit, PolicyEngine, RootPolicyError, load_root_policy
from eios_policy.root_policy import canonical_digest, lock_path, write_lock

REPO = Path(__file__).resolve().parents[2]
POLICY = REPO / "policy" / "root_policy.yaml"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "a.py").write_text("x=1\n")
    return ws


@pytest.fixture
def engine(workspace: Path, tmp_path: Path) -> PolicyEngine:
    return PolicyEngine(
        load_root_policy(POLICY),
        audit=NullAudit(),
        approvals=NoApprovals(),
        workspace_roots=[workspace],
        sandbox_output_dir=tmp_path / "out",
    )


def req(action: str, **kw: object) -> PolicyRequest:
    attrs = kw.pop("attributes", {})
    return PolicyRequest(
        actor_type=kw.pop("actor_type", ActorType.LLM),  # type: ignore[arg-type]
        actor_id="agent-1", action=action, attributes=attrs, **kw,  # type: ignore[arg-type]
    )  # fmt: skip


async def effect(engine: PolicyEngine, request: PolicyRequest) -> tuple[PolicyEffect, str]:
    d = await engine.evaluate(request)
    return d.effect, d.rule_id


# ---- external database: unconditionally denied ----
@pytest.mark.parametrize(
    "capability",
    ["external_database_connect", "external_database_read", "external_database_write",
     "external_database_execute", "external_database_ddl", "EXTERNAL_DATABASE_READ",
     "external_database_new_thing"],
)  # fmt: skip
async def test_forbidden_capabilities_always_denied(engine: PolicyEngine, capability: str) -> None:
    for attrs in ({}, {"provider_state": "TRUSTED", "provider_approved": True, "risk": "low"}):
        got = await effect(
            engine, req("capability.invoke", capability=capability, attributes=attrs)
        )
        assert got == (PolicyEffect.DENY, "root.forbidden_capability")


@pytest.mark.parametrize(
    "url",
    ["postgresql+psycopg://u:p@prod-db.corp.example:5432/app", "postgresql+psycopg://u:p@10.0.0.5/x",
     "mysql://u:p@db/x", "mssql+pyodbc://u:p@sql01/x", "postgresql+psycopg://u:p@postgres.evil.com/x"],
)  # fmt: skip
async def test_db_connect_to_anything_external_is_denied(engine: PolicyEngine, url: str) -> None:
    for actor in (ActorType.LLM, ActorType.AGENT, ActorType.TOOL, ActorType.SYSTEM):
        got = await effect(engine, req("db.connect", target=url, actor_type=actor))
        assert got == (PolicyEffect.DENY, "root.external_database")


async def test_even_the_internal_database_is_closed_to_llms_agents_and_tools(
    engine: PolicyEngine,
) -> None:
    url = "postgresql+psycopg://u:p@postgres:5432/eios"
    for actor in (ActorType.LLM, ActorType.AGENT, ActorType.TOOL):
        assert (await effect(engine, req("db.connect", target=url, actor_type=actor)))[
            0
        ] is PolicyEffect.DENY
    assert (await effect(engine, req("db.connect", target=url, actor_type=ActorType.SYSTEM)))[
        0
    ] is PolicyEffect.ALLOW


@pytest.mark.parametrize(
    "action", ["db.read", "db.write", "db.execute", "db.ddl", "sql.database_execute"]
)
async def test_every_database_action_is_denied(engine: PolicyEngine, action: str) -> None:
    got = await effect(engine, req(action, target="anything"))
    assert got == (PolicyEffect.DENY, "root.external_database")


@pytest.mark.parametrize("port", [1433, 1521, 3306, 5432, 27017, 6379])
async def test_connections_to_database_ports_are_denied_even_if_allowlisted(
    engine: PolicyEngine, port: int
) -> None:
    attrs = {"network_allowed": True, "allowed_hosts": ["db.partner.example"]}
    got = await effect(
        engine, req("net.connect", target=f"db.partner.example:{port}", attributes=attrs)
    )
    assert got == (PolicyEffect.DENY, "root.external_database")


@pytest.mark.parametrize(
    "binary", ["psql", "/usr/bin/psql", "mysql", "sqlcmd", "sqlplus", "mongosh", "redis-cli", "bcp"]
)
async def test_database_client_binaries_never_run_even_when_allowlisted(
    engine: PolicyEngine, binary: str
) -> None:
    attrs = {"allowed_processes": [binary], "registered": True, "argv": [binary]}
    got = await effect(engine, req("process.exec", target=binary, attributes=attrs))
    assert got == (PolicyEffect.DENY, "root.external_database")


async def test_indirect_database_client_execution_is_denied(engine: PolicyEngine) -> None:
    attrs = {
        "allowed_processes": ["/bin/sh"],
        "registered": True,
        "argv": ["/bin/sh", "-c", "psql"],
    }
    # `sh -c psql`: the argument's basename is a DB client
    attrs["argv"] = ["/bin/sh", "/usr/bin/psql"]
    got = await effect(engine, req("process.exec", target="/bin/sh", attributes=attrs))
    assert got == (PolicyEffect.DENY, "root.external_database")


# ---- approvals ----
@pytest.mark.parametrize(
    "action",
    ["git.push", "git.merge", "git.rebase", "git.reset_hard", "git.release", "git.force_push",
     "git.history_rewrite"],
)  # fmt: skip
async def test_dangerous_git_actions_need_human_approval(engine: PolicyEngine, action: str) -> None:
    d = await engine.evaluate(req(action, target="origin/main"))
    assert d.effect is PolicyEffect.REQUIRE_APPROVAL and d.rule_id == "root.human_approval"
    assert d.approval_id is not None


async def test_unknown_actions_are_denied_by_default(engine: PolicyEngine) -> None:
    assert await effect(engine, req("teleport.everything")) == (PolicyEffect.DENY, "default.deny")


# ---- filesystem ----
async def test_filesystem_scopes(engine: PolicyEngine, workspace: Path, tmp_path: Path) -> None:
    ok = str(workspace / "src" / "a.py")
    assert (await effect(engine, req("fs.read", target=ok)))[0] is PolicyEffect.ALLOW
    assert (await effect(engine, req("fs.write", target=ok)))[1] == "fs.write_forbidden"
    write_ok = req("fs.write", target=ok, attributes={"actor_can_write": True})
    assert (await effect(engine, write_ok))[0] is PolicyEffect.ALLOW
    assert (await effect(engine, req("fs.read", target="/etc/hostname")))[1] == "fs.outside_scope"
    assert (await effect(engine, req("fs.write", target=str(tmp_path / "out" / "r.json"))))[
        0
    ] is PolicyEffect.ALLOW


async def test_path_traversal_and_symlinks_cannot_escape(
    engine: PolicyEngine, workspace: Path, tmp_path: Path
) -> None:
    traversal = str(workspace / "src" / ".." / ".." / ".." / "etc" / "hostname")
    assert (await effect(engine, req("fs.read", target=traversal)))[0] is PolicyEffect.DENY
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("s")
    link = workspace / "src" / "innocent.txt"
    link.symlink_to(secret)
    assert (await effect(engine, req("fs.read", target=str(link))))[1] == "fs.outside_scope"
    assert (await effect(engine, req("fs.read", target=str(workspace) + "\x00/../x")))[
        0
    ] is PolicyEffect.DENY
    sibling = tmp_path / "ws-evil"
    sibling.mkdir()
    assert (await effect(engine, req("fs.read", target=str(sibling))))[
        0
    ] is PolicyEffect.DENY  # prefix trick


@pytest.mark.parametrize(
    "rel", [".env", ".git/hooks/pre-commit", ".ssh/id_rsa", "credentials.json", ".aws/credentials"]
)
async def test_secret_looking_paths_are_denied_inside_the_workspace(
    engine: PolicyEngine, workspace: Path, rel: str
) -> None:
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x")
    for action in ("fs.read", "fs.write"):
        d = await engine.evaluate(
            req(action, target=str(target), attributes={"actor_can_write": True})
        )
        assert d.effect is PolicyEffect.DENY, rel


async def test_git_internals_are_not_writable(engine: PolicyEngine, workspace: Path) -> None:
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("")
    d = await engine.evaluate(
        req(
            "fs.write",
            target=str(workspace / ".git" / "config"),
            attributes={"actor_can_write": True},
        )
    )
    assert d.effect is PolicyEffect.DENY


# ---- network ----
async def test_network_rules(engine: PolicyEngine) -> None:
    on = {"network_allowed": True, "allowed_hosts": ["api.example.org", "*.docs.example.org"]}
    assert (
        await effect(engine, req("net.connect", target="https://api.example.org/x", attributes=on))
    )[0] is PolicyEffect.ALLOW
    assert (
        await effect(engine, req("net.connect", target="a.docs.example.org:443", attributes=on))
    )[0] is PolicyEffect.ALLOW
    assert (await effect(engine, req("net.connect", target="evil.example.org", attributes=on)))[
        1
    ] == "net.not_allowlisted"
    assert (await effect(engine, req("net.connect", target="docs.example.org", attributes=on)))[
        0
    ] is PolicyEffect.DENY
    assert (await effect(engine, req("net.connect", target="api.example.org")))[1] == "net.disabled"
    for host in (
        "postgres",
        "localhost",
        "127.0.0.1",
        "169.254.169.254",
        "10.1.2.3",
        "192.168.1.9",
        "[::1]",
    ):
        d = await engine.evaluate(
            req("net.connect", target=host, attributes={**on, "allowed_hosts": [host]})
        )
        assert d.effect is PolicyEffect.DENY, host


# ---- processes & secrets ----
async def test_unregistered_or_unlisted_executables_never_run(engine: PolicyEngine) -> None:
    ok = {"allowed_processes": ["/opt/tool"], "registered": True, "argv": ["/opt/tool"]}
    assert (await effect(engine, req("process.exec", target="/opt/tool", attributes=ok)))[
        0
    ] is PolicyEffect.ALLOW
    assert (await effect(engine, req("process.exec", target="/bin/sh", attributes=ok)))[
        1
    ] == "process.not_allowlisted"
    unreg = {**ok, "registered": False}
    assert (await effect(engine, req("process.exec", target="/opt/tool", attributes=unreg)))[
        1
    ] == "process.unregistered"


async def test_secret_requests_need_a_grant(engine: PolicyEngine) -> None:
    assert (await effect(engine, req("secret.read", target="API_KEY")))[1] == "secret.not_granted"
    granted = req("secret.read", target="API_KEY", attributes={"allowed_secrets": ["API_KEY"]})
    assert (await effect(engine, granted))[0] is PolicyEffect.ALLOW


# ---- capability invocation ----
async def test_invoke_rules(engine: PolicyEngine) -> None:
    base = {"provider_state": "TRUSTED", "provider_origin": "builtin", "risk": "low"}
    assert (await effect(engine, req("capability.invoke", capability="x", attributes=base)))[
        0
    ] is PolicyEffect.ALLOW
    for state in ("QUARANTINED", "BROKEN", "DISABLED", "UNREGISTERED", ""):
        d = await engine.evaluate(
            req("capability.invoke", capability="x", attributes={**base, "provider_state": state})
        )
        assert d.effect is PolicyEffect.DENY
    gen = {**base, "provider_origin": "generated", "provider_state": "EXPERIMENTAL"}
    assert (await effect(engine, req("capability.invoke", capability="x", attributes=gen)))[
        1
    ] == "capability.unapproved"
    exp_high = {**base, "provider_state": "EXPERIMENTAL", "risk": "high"}
    assert (await effect(engine, req("capability.invoke", capability="x", attributes=exp_high)))[
        0
    ] is PolicyEffect.REQUIRE_APPROVAL
    exp_low = {**base, "provider_state": "EXPERIMENTAL", "risk": "low"}
    assert (await effect(engine, req("capability.invoke", capability="x", attributes=exp_low)))[
        0
    ] is PolicyEffect.ALLOW


async def test_every_decision_is_audited_but_dry_runs_are_not(workspace: Path) -> None:
    audit = NullAudit()
    eng = PolicyEngine(
        load_root_policy(POLICY), audit=audit, approvals=NoApprovals(), workspace_roots=[workspace]
    )
    await eng.evaluate(req("db.read"))
    await eng.evaluate(req("db.read"), dry_run=True)
    assert len(audit.decisions) == 1 and audit.decisions[0].effect is PolicyEffect.DENY


# ---- Root Policy immutability ----
def test_root_policy_is_frozen_and_summary_says_immutable() -> None:
    root = load_root_policy(POLICY)
    with pytest.raises((TypeError, ValueError)):
        root.version = "9"
    with pytest.raises(AttributeError):
        root.forbidden_capabilities.add("x")  # type: ignore[attr-defined]
    assert root.summary()["mutable_at_runtime"] is False


def _copy_policy(tmp_path: Path) -> Path:
    target = tmp_path / "root_policy.yaml"
    target.write_text(POLICY.read_text())
    lock_path(target).write_text(lock_path(POLICY).read_text())
    return target


def test_tampered_policy_refuses_to_load(tmp_path: Path) -> None:
    target = _copy_policy(tmp_path)
    load_root_policy(target)  # sanity
    target.write_text(target.read_text().replace("- psql\n", "- nothing\n"))
    with pytest.raises(RootPolicyError, match="digest"):
        load_root_policy(target)


def test_missing_lock_or_symlinked_policy_refuses_to_load(tmp_path: Path) -> None:
    target = _copy_policy(tmp_path)
    lock_path(target).unlink()
    with pytest.raises(RootPolicyError):
        load_root_policy(target)
    link = tmp_path / "link.yaml"
    link.symlink_to(POLICY)
    with pytest.raises(RootPolicyError):
        load_root_policy(link)


def test_a_relocked_weakened_policy_still_keeps_compiled_in_invariants(tmp_path: Path) -> None:
    target = tmp_path / "weak.yaml"
    doc = {
        "version": "6.6.6",
        "forbidden_capabilities": [],
        "database_client_binaries": [],
        "database_ports": [],
        "approval_required_actions": [],
    }
    target.write_text(yaml.safe_dump(doc))
    write_lock(target)
    weak = load_root_policy(target)
    assert weak.is_forbidden_capability("external_database_read")
    assert "psql" in weak.database_client_binaries and 5432 in weak.database_ports
    assert "git.push" in weak.approval_required_actions
    assert canonical_digest(dict[str, object](doc)) == weak.digest


def test_no_api_route_can_write_the_root_policy() -> None:
    from eios_api.app import create_app
    from tests.conftest import make_settings

    app = create_app(make_settings(), readiness_checks=[])
    for route in app.routes:
        path = getattr(route, "path", "")
        methods: set[str] = getattr(route, "methods", None) or set()
        if "policy" in path:
            assert methods <= {"GET", "HEAD", "POST"}
            if "POST" in methods:
                assert path == "/policy/evaluate"  # dry-run only
    assert os.access(POLICY, os.R_OK)
