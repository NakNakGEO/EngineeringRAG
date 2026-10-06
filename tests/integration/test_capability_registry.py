from __future__ import annotations

import textwrap
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_capability import RoutingRequest, RoutingStatus, load_directory
from eios_domain.registry import Origin, RegistryState, TransitionError
from eios_runtime import Container, build_container
from tests.conftest import make_settings

pytestmark = pytest.mark.integration
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
async def container(db: AsyncEngine, tmp_path: Path) -> Container:
    c = build_container(
        make_settings(manifests_dir=REPO / "manifests", blob_dir=tmp_path / "blobs"), db
    )
    report = await c.sync_registries()
    assert report.ok, report.rejected
    return c


def write(tmp: Path, name: str, body: str) -> Path:
    path = tmp / name
    path.write_text(textwrap.dedent(body))
    return path


async def test_builtin_sync_is_idempotent_and_healthy(container: Container) -> None:
    again = await container.sync_registries()
    assert again.added == [] and again.updated == [] and again.ok
    providers = await container.registry.provider_summaries()
    assert len(providers) == 8
    assert all(p["health"] == "ok" and p["state"] == "TRUSTED" for p in providers)
    caps = {c["id"]: c for c in await container.registry.capability_summaries()}
    assert caps["sql_analyze"]["providers"] == 1
    assert caps["file_edit"]["providers"] == 0  # declared but nothing implements it yet


async def test_router_selects_registered_provider_and_never_guesses(container: Container) -> None:
    ok = await container.router.resolve(RoutingRequest(capability="sql_analyze", language="sql"))
    assert ok.status is RoutingStatus.SELECTED and ok.selected
    assert ok.selected.provider.id == "sql_analyze"
    gap = await container.router.resolve(RoutingRequest(capability="file_edit"))
    assert gap.status is RoutingStatus.NO_PROVIDER and gap.selected is None
    unknown = await container.router.resolve(RoutingRequest(capability="teleport_code"))
    assert unknown.status is RoutingStatus.UNKNOWN_CAPABILITY
    forbidden = await container.router.resolve(RoutingRequest(capability="external_database_read"))
    assert forbidden.status is RoutingStatus.FORBIDDEN


async def test_router_emits_events_on_a_run(container: Container) -> None:
    async with container.recorder.run(kind="route", goal="route") as ctx:
        await container.router.resolve(RoutingRequest(capability="git_inspect"), ctx)
        await container.router.resolve(RoutingRequest(capability="file_edit"), ctx)
    types = [e.type for e in (await container.events.list_events(ctx.run_id)).items]
    assert "CAPABILITY_REQUESTED" in types and "TOOL_SELECTED" in types
    assert "CAPABILITY_GAP_DETECTED" in types


async def test_unhealthy_or_demoted_providers_are_not_routed(container: Container) -> None:
    store = container.registry.store
    await container.registry.transition(
        "provider",
        "git_inspect",
        "1.0.0",
        RegistryState.QUARANTINED,
        actor="sec",
        reason="suspicious output",
    )
    decision = await container.router.resolve(RoutingRequest(capability="git_inspect"))
    assert decision.status is RoutingStatus.NO_PROVIDER
    assert any("QUARANTINED" in r for _, r in decision.rejected)
    with pytest.raises(TransitionError):
        await container.registry.transition(
            "provider", "git_inspect", "1.0.0", RegistryState.TRUSTED, actor="bot", reason="x"
        )
    history = await store.history("provider", "git_inspect")
    assert history[0]["to_state"] == "QUARANTINED" and history[0]["actor"] == "sec"
    # a re-sync must not undo the quarantine
    await container.sync_registries()
    again = await store.get_provider("git_inspect")
    assert again is not None and again.state is RegistryState.QUARANTINED


async def test_missing_implementation_is_reported_unhealthy(
    container: Container, tmp_path: Path
) -> None:
    write(
        tmp_path,
        "t.yaml",
        """
        kind: tool
        id: ghost_tool
        version: 1.0.0
        description: Claims an adapter that does not exist.
        capabilities: [source_read]
        entrypoint: builtin:does_not_exist
        state: TRUSTED
    """,
    )
    report = await container.registry.sync(load_directory(tmp_path), origin=Origin.BUILTIN)
    assert report.ok
    await container.health.run_all()
    ghost = await container.registry.store.get_provider("ghost_tool")
    assert ghost is not None and ghost.health_status == "fail"
    decision = await container.router.resolve(
        RoutingRequest(capability="source_read", require_provider="ghost_tool")
    )
    assert decision.status is RoutingStatus.NO_PROVIDER


async def test_plugin_cannot_claim_trust_and_versions_are_immutable(
    container: Container, tmp_path: Path
) -> None:
    body = """
        kind: tool
        id: plugin_lint
        version: 1.0.0
        description: A plugin supplied tool.
        capabilities: [lint_run]
        entrypoint: builtin:knowledge_search
        state: TRUSTED
        trust: owner
    """
    write(tmp_path, "p.yaml", body)
    report = await container.registry.sync(load_directory(tmp_path), origin=Origin.PLUGIN)
    assert report.ok
    row = await container.registry.store.get_provider("plugin_lint")
    assert row is not None and row.state is RegistryState.EXPERIMENTAL  # clamped by origin
    write(tmp_path, "p.yaml", body.replace("A plugin supplied", "A modified plugin supplied"))
    changed = await container.registry.sync(load_directory(tmp_path), origin=Origin.PLUGIN)
    assert changed.rejected and "immutable" in changed.rejected[0][1]


async def test_generated_provider_is_not_routable_until_human_approved(
    container: Container, tmp_path: Path
) -> None:
    write(
        tmp_path,
        "g.yaml",
        """
        kind: tool
        id: generated_lint
        version: 1.0.0
        description: A tool produced by the workshop.
        capabilities: [lint_run]
        entrypoint: builtin:knowledge_search
    """,
    )
    await container.registry.sync(load_directory(tmp_path), origin=Origin.GENERATED)
    req = RoutingRequest(capability="lint_run")
    blocked = await container.router.resolve(req)
    assert blocked.status is RoutingStatus.NO_PROVIDER
    assert any("approval" in r for _, r in blocked.rejected)
    await container.registry.transition(
        "provider",
        "generated_lint",
        "1.0.0",
        RegistryState.EXPERIMENTAL,
        actor="owner",
        reason="reviewed",
        human_approved=True,
    )
    assert (await container.router.resolve(req)).status is RoutingStatus.SELECTED


async def test_unknown_capability_reference_is_rejected(
    container: Container, tmp_path: Path
) -> None:
    write(
        tmp_path,
        "t.yaml",
        """
        kind: tool
        id: orphan_tool
        version: 1.0.0
        description: References a capability nobody defined.
        capabilities: [not_registered_anywhere]
        entrypoint: builtin:knowledge_search
    """,
    )
    report = await container.registry.sync(load_directory(tmp_path), origin=Origin.BUILTIN)
    assert report.rejected and "unknown capabilities" in report.rejected[0][1]
    assert await container.registry.store.get_provider("orphan_tool") is None


async def test_non_builtin_writer_agent_waits_for_human(
    container: Container, tmp_path: Path
) -> None:
    write(
        tmp_path,
        "a.yaml",
        """
        kind: agent
        id: plugin_writer
        version: 1.0.0
        role: Plugin Writer
        description: A writer agent from a plugin.
        prompt: You edit files carefully inside the approved workspace only.
        can_write: true
        permissions: {write: true}
    """,
    )
    await container.registry.sync(load_directory(tmp_path), origin=Origin.PLUGIN)
    agent = await container.registry.store.get_agent("plugin_writer")
    assert agent is not None and agent["state"] == "DISABLED"


async def test_metrics_influence_routing(container: Container, tmp_path: Path) -> None:
    write(
        tmp_path,
        "t.yaml",
        """
        kind: tool
        id: alt_reader
        version: 1.0.0
        description: Alternative source reader.
        capabilities: [source_read]
        entrypoint: builtin:source_read
        state: TRUSTED
        priority: 60
    """,
    )
    await container.registry.sync(load_directory(tmp_path), origin=Origin.BUILTIN)
    await container.health.run_all()
    store = container.registry.store
    for _ in range(5):
        await store.record_outcome("source_read", "1.0.0", "source_read", success=False)
        await store.record_outcome("alt_reader", "1.0.0", "source_read", success=True)
    decision = await container.router.resolve(RoutingRequest(capability="source_read"))
    assert decision.selected is not None and decision.selected.provider.id == "alt_reader"


async def test_registry_api_is_compact_with_lazy_detail(client: httpx.AsyncClient) -> None:
    listing = await client.get("/capabilities")
    assert listing.status_code == 200
    items = listing.json()
    assert items and set(items[0]) == {"id", "description", "risk", "providers"}
    detail = await client.get("/capabilities/sql_analyze")
    assert detail.status_code == 200 and detail.json()["providers"][0]["id"] == "sql_analyze"
    assert (await client.get("/capabilities/nope")).status_code == 404
    resolved = await client.post("/capabilities/resolve", json={"capability": "git_inspect"})
    assert resolved.json()["status"] == "selected"
    gap = await client.post("/capabilities/resolve", json={"capability": "teleport"})
    assert gap.json()["status"] == "unknown_capability"
    assert len((await client.get("/agents")).json()) == 16
    assert len((await client.get("/skills")).json()) == 7
    assert (await client.get("/tools/sql_analyze")).json()["health_status"] == "ok"
    assert (await client.get("/agents/software_engineer")).json()["can_write"] is True
    assert (await client.get("/skills/nope")).status_code == 404


async def test_http_invoke_runs_registered_tools_and_refuses_everything_else(
    client: httpx.AsyncClient,
) -> None:
    ok = await client.post(
        "/capabilities/invoke",
        json={"capability": "sql_analyze", "arguments": {"sql": "CREATE TABLE t (id int);"}},
    )
    assert ok.status_code == 200 and ok.json()["status"] == "ok"
    for cap, expected in (
        ("external_database_execute", "denied"),
        ("file_edit", "no_provider"),
        ("run_shell", "no_provider"),
    ):
        got = (await client.post("/capabilities/invoke", json={"capability": cap})).json()
        assert got["status"] == expected and got["output"] is None
