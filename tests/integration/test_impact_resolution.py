"""Impact and call resolution on a mixed Python/TypeScript repository (dogfood findings 4 and 8)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.policy import Risk
from eios_retrieval.impact import WEAK_VIA
from eios_runtime import Container, build_container
from tests.conftest import make_settings
from tests.gitfixtures import make_repo

pytestmark = pytest.mark.integration

FILES: dict[str, str | bytes] = {
    "README.md": "# Mixed\n",
    "docs/README.md": "# Docs\n",
    "pkg/__init__.py": "from pkg.engine import Engine\nfrom pkg.events import EventType\n",
    "pkg/events.py": ("class EventType:\n    RUN = 'run'\n\n\nclass Other:\n    pass\n"),
    # a class referenced by attribute leaves no call edge: only the named import shows the use
    "app/uses_events.py": (
        "from pkg.events import EventType\n\n\ndef label():\n    return EventType.RUN\n"
    ),
    "app/uses_other.py": "from pkg.events import Other\n\n\nTHING = Other\n",
    "app/via_hub.py": "from pkg import EventType\n\n\nKIND = EventType.RUN\n",
    # calls something named like the migration entry point; it is not a caller of the migration
    "app/runner.py": "def go(tool):\n    return tool.upgrade(1)\n",
    "pkg/engine.py": (
        "class Engine:\n    def evaluate(self, x):\n        return str(x)\n"
        "\n\ndef start():\n    return Engine()\n"
    ),
    "pkg/other.py": "class Other:\n    def evaluate(self, x):\n        return x\n",
    # imports Engine through the package: the re-export must be followed to engine.py
    "app/service.py": ("from pkg import Engine\n\n\ndef run():\n    return Engine().evaluate(1)\n"),
    # receivers of unknown type: 'evaluate' is defined twice, so these cannot be placed
    "app/a_late.py": "def lateral(obj):\n    return obj.evaluate(2)\n",
    "app/b_late.py": "def lateral_two(obj):\n    return obj.evaluate(3)\n",
    "app/routes/items.py": "def list_items():\n    return []\n",
    "policy/guard.py": "def check(x):\n    return x\n",
    "web/src/str.ts": "export const str = 1;\nexport function render() { return str; }\n",
    "migrations/versions/0001_audit.py": (
        "def upgrade():\n"
        "    op.execute('CREATE TRIGGER audit_append_only BEFORE UPDATE ON audit')\n"
    ),
    "tests/test_migrations.py": "def test_upgrade_runs():\n    assert True\n",
    "tests/test_engine.py": "from pkg.engine import Engine\n\n\ndef test_it():\n    Engine()\n",
}


@pytest.fixture
async def mixed(db: AsyncEngine, tmp_path: Path) -> tuple[Container, object]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    container = build_container(
        make_settings(workspace_roots=str(workspace), blob_dir=tmp_path / "blobs"), db
    )
    repo = make_repo(workspace, "mixed", dict(FILES))
    boot = await container.projects.bootstrap(repo)
    assert boot.project is not None
    await container.projects.sync_now(boot.project.id)
    return container, boot.project.id


async def _edges(container: Container, project_id: object, src: str) -> list[dict[str, object]]:
    project = await container.projects.require(project_id)  # type: ignore[arg-type]
    return await container.projects.store.edges_from(
        project.id, project.last_branch or "main", [src], kinds=["calls"]
    )


async def test_a_python_builtin_never_resolves_into_typescript(
    mixed: tuple[Container, object],
) -> None:
    container, pid = mixed
    edges = await _edges(container, pid, "symbol:pkg/engine.py::Engine.evaluate")
    assert not [e for e in edges if str(e["dst_key"]).startswith("symbol:web/")]
    assert not [e for e in edges if str(e["dst_key"]).startswith("callee:str")]  # no noise either


async def test_a_reexported_import_resolves_to_the_defining_file_at_high_confidence(
    mixed: tuple[Container, object],
) -> None:
    container, pid = mixed
    edges = await _edges(container, pid, "symbol:app/service.py::run")
    hits = [e for e in edges if e["dst_key"] == "symbol:pkg/engine.py::Engine.evaluate"]
    assert hits and hits[0]["confidence"] == 0.8


async def test_callers_that_could_not_be_placed_are_listed_flagged_and_not_counted(
    mixed: tuple[Container, object],
) -> None:
    container, pid = mixed
    report = await container.impact.analyze(pid, symbols=["Engine.evaluate"])  # type: ignore[arg-type]
    solid = {e.path for e in report.callers if e.via != WEAK_VIA}
    weak = {e.path: e for e in report.callers if e.via == WEAK_VIA}
    assert "app/service.py" in solid
    assert {"app/a_late.py", "app/b_late.py"} <= set(weak)
    assert all(e.low_confidence and e.confidence == 0.2 for e in weak.values())
    assert report.low_confidence_edges >= 2
    assert "app/a_late.py" not in report.consumers  # not part of the blast radius
    assert any("matched by name only" in f for f in report.risk_factors)
    both = await container.impact.analyze(pid, symbols=["evaluate"])  # type: ignore[arg-type]
    assert set(both.ambiguous["evaluate"]) == {
        "pkg/engine.py::Engine.evaluate",
        "pkg/other.py::Other.evaluate",
    }
    assert any("ambiguous" in w and "definitions match" in w for w in both.warnings)


async def test_security_critical_code_is_high_and_a_migration_is_a_database_change(
    mixed: tuple[Container, object],
) -> None:
    container, pid = mixed
    guard = await container.impact.analyze(pid, paths=["policy/guard.py"])  # type: ignore[arg-type]
    assert guard.risk in {Risk.HIGH, Risk.CRITICAL}
    assert any("security-critical" in f for f in guard.risk_factors)

    migration = await container.impact.analyze(
        pid,  # type: ignore[arg-type]
        paths=["migrations/versions/0001_audit.py"],
    )
    assert migration.risk in {Risk.MEDIUM, Risk.HIGH, Risk.CRITICAL}
    assert migration.db_scripts == ["migrations/versions/0001_audit.py"]
    assert "tests/test_migrations.py" in migration.tests and not migration.tests_missing
    assert any("database schema" in f for f in migration.risk_factors)


async def test_nothing_resolved_is_medium_risk_and_says_so(
    mixed: tuple[Container, object],
) -> None:
    container, pid = mixed
    report = await container.impact.analyze(pid, paths=["no/such/file.py"])  # type: ignore[arg-type]
    assert report.resolved is False and report.risk is Risk.MEDIUM and report.confidence == 0.0
    assert any("cannot be assessed" in f for f in report.risk_factors)


async def test_an_ambiguous_path_names_every_match(mixed: tuple[Container, object]) -> None:
    container, pid = mixed
    report = await container.impact.analyze(pid, paths=["README.md"])  # type: ignore[arg-type]
    assert set(report.ambiguous["README.md"]) == {"README.md", "docs/README.md"}
    assert any("ambiguous" in w and "only the exact path" in w for w in report.warnings)


async def test_a_route_module_without_import_tests_is_explained_not_blamed(
    mixed: tuple[Container, object],
) -> None:
    container, pid = mixed
    report = await container.impact.analyze(pid, paths=["app/routes/items.py"])  # type: ignore[arg-type]
    assert report.tests_missing
    assert any("tested over HTTP only" in f for f in report.risk_factors)


async def test_symbol_consumers_exclude_importers_of_the_file(
    mixed: tuple[Container, object],
) -> None:
    container, pid = mixed
    report = await container.impact.analyze(pid, symbols=["start"])  # type: ignore[arg-type]
    assert "tests/test_engine.py" not in report.consumers  # imports engine.py, never calls start
    assert any(e.path == "tests/test_engine.py" for e in report.importers)


async def test_a_class_used_by_attribute_has_consumers_through_named_imports(
    mixed: tuple[Container, object],
) -> None:
    container, pid = mixed
    report = await container.impact.analyze(pid, symbols=["EventType"])  # type: ignore[arg-type]
    assert {"app/uses_events.py", "app/via_hub.py"} <= set(report.consumers)  # one hop via __init__
    assert "app/uses_other.py" not in report.consumers  # imports the module, not this class
    assert any(e.path == "app/uses_other.py" for e in report.importers)
    assert report.blast_radius == len(report.consumers) >= 2


async def test_documents_have_no_missing_tests_and_migrations_no_phantom_callers(
    mixed: tuple[Container, object],
) -> None:
    container, pid = mixed
    readme = await container.impact.analyze(pid, paths=["docs/README.md"])  # type: ignore[arg-type]
    assert readme.tests_missing is False
    migration = await container.impact.analyze(
        pid,  # type: ignore[arg-type]
        paths=["migrations/versions/0001_audit.py"],
    )
    # migration entry points are invoked by the tool: name-only matching them is pure noise
    assert not [e for e in migration.callers if e.via == WEAK_VIA]
