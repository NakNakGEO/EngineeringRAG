"""Phase 3: identity, git state, incremental indexing, overlays, graph - on real git repos."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.events import EventType
from eios_domain.project import BootstrapState, FileScope
from eios_project_intelligence import (
    ApprovedWorkspaces,
    ProjectIndexer,
    ProjectService,
    ProjectStore,
    WorkspaceViolationError,
)
from eios_project_intelligence.parsers import ParserRegistry, SourceParser
from eios_runtime import Container, build_container
from tests.conftest import make_settings
from tests.gitfixtures import MINI_PROJECT, commit_all, git, make_repo, write

pytestmark = pytest.mark.integration


class CountingRegistry(ParserRegistry):
    """Records which files were parsed so tests can prove only changed files are reprocessed."""

    def __init__(self) -> None:
        super().__init__()
        self.parsed: list[str] = []

    def get(self, language: str) -> Any:
        found = super().get(language)
        if found is None:
            return None
        parser: SourceParser = found
        outer = self

        class Spy:
            language = parser.language

            def parse(self, path: str, text: str) -> Any:
                outer.parsed.append(path)
                return parser.parse(path, text)

            def import_keys(self, path: str, imp: Any) -> list[str]:
                return parser.import_keys(path, imp)

        return Spy()


class Env:
    def __init__(self, engine: AsyncEngine, root: Path) -> None:
        self.root = root
        self.registry = CountingRegistry()
        self.store = ProjectStore(engine)
        self.workspaces = ApprovedWorkspaces([root])
        self.indexer = ProjectIndexer(self.store, self.workspaces, self.registry)
        self.container: Container = build_container(
            make_settings(workspace_roots=str(root), blob_dir=root / ".blobs"), engine
        )
        # use the counting indexer inside the service too
        self.service = ProjectService(
            self.store, self.indexer, self.container.queue, self.container.recorder
        )

    def reset_counter(self) -> None:
        self.registry.parsed.clear()


@pytest.fixture
def env(db: AsyncEngine, tmp_path: Path) -> Env:
    root = tmp_path / "workspace"
    root.mkdir()
    return Env(db, root)


async def _keys(
    env: Env, project_id: Any, kind: str | None = None, **kw: Any
) -> set[tuple[str, str]]:
    project = await env.store.get_project(project_id)
    assert project and project.last_branch
    edges = await env.store.edges_from(
        project_id, project.last_branch, kw.pop("srcs"), kinds=[kind] if kind else None, **kw
    )
    return {(e["src_key"], e["dst_key"]) for e in edges}


# ---------------------------------------------------------------------------- bootstrap states
async def test_bootstrap_state_machine(env: Env) -> None:
    repo = make_repo(
        env.root, "proj", MINI_PROJECT, remote="https://tok:secret@github.com/Acme/Proj.git"
    )
    first = await env.indexer.bootstrap(repo)
    assert first.state is BootstrapState.NEW and first.project is not None
    assert first.project.remote == "github.com/acme/proj"  # normalised, credentials stripped
    assert "secret" not in str(first.project)
    assert first.needs_sync and first.branch == "main"
    assert await env.indexer.bootstrap(repo) is not None  # idempotent identity
    pid = first.project.id

    result = await env.indexer.sync(pid)
    assert result.state is BootstrapState.CURRENT and result.files_total == len(MINI_PROJECT)
    again = await env.indexer.bootstrap(repo)
    assert again.state is BootstrapState.CURRENT and not again.needs_sync
    assert again.project and again.project.id == pid

    write(repo, {"app/models.py": str(MINI_PROJECT["app/models.py"]) + "\n# edited\n"})
    dirty = await env.indexer.bootstrap(repo)
    assert dirty.state is BootstrapState.DIRTY and dirty.dirty_paths == 1

    commit_all(repo, "edit")
    stale = await env.indexer.bootstrap(repo)
    assert stale.state is BootstrapState.STALE and "1 files changed" in (stale.detail or "")
    await env.indexer.sync(pid)
    assert (await env.indexer.bootstrap(repo)).state is BootstrapState.CURRENT

    git(repo, "checkout", "-q", "-b", "feature")
    assert (await env.indexer.bootstrap(repo)).state is BootstrapState.BRANCH_CHANGED
    await env.indexer.sync(pid)
    assert (await env.indexer.bootstrap(repo)).state is BootstrapState.CURRENT


async def test_force_rewrite_is_major_divergence(env: Env) -> None:
    repo = make_repo(env.root, "proj", MINI_PROJECT)
    boot = await env.indexer.bootstrap(repo)
    assert boot.project
    write(repo, {"extra.py": "a = 1\n"})
    commit_all(repo, "second")
    await env.indexer.sync(boot.project.id)
    git(repo, "reset", "-q", "--hard", "HEAD~1")
    write(repo, {"other.py": "b = 2\n"})
    commit_all(repo, "rewritten")  # the indexed commit is no longer an ancestor
    div = await env.indexer.bootstrap(repo)
    assert div.state is BootstrapState.MAJOR_DIVERGENCE
    result = await env.indexer.sync(boot.project.id)  # sync is hash-based, so it still converges
    assert "other.py" in result.changed and "extra.py" in result.removed


async def test_bootstrap_errors_and_workspace_enforcement(env: Env, tmp_path: Path) -> None:
    plain = env.root / "not-a-repo"
    plain.mkdir()
    err = await env.indexer.bootstrap(plain)
    assert err.state is BootstrapState.ERROR and err.project is None and err.detail

    empty = env.root / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    assert (await env.indexer.bootstrap(empty)).state is BootstrapState.ERROR  # no commits

    outside = make_repo(tmp_path, "outside", MINI_PROJECT)
    with pytest.raises(WorkspaceViolationError):
        await env.indexer.bootstrap(outside)
    with pytest.raises(WorkspaceViolationError):
        await env.indexer.bootstrap(env.root / ".." / "outside")


async def test_subdirectory_resolves_to_the_repository_project(env: Env) -> None:
    repo = make_repo(env.root, "proj", MINI_PROJECT)
    top = await env.indexer.bootstrap(repo)
    sub = await env.indexer.bootstrap(repo / "app")
    assert top.project and sub.project and top.project.id == sub.project.id


async def test_identity_survives_moving_the_project(env: Env) -> None:
    repo = make_repo(env.root, "proj", MINI_PROJECT, remote="git@github.com:Acme/Proj.git")
    first = await env.indexer.bootstrap(repo)
    assert first.project
    moved = env.root / "renamed"
    shutil.move(str(repo), str(moved))
    second = await env.indexer.bootstrap(moved)
    assert second.project and second.project.id == first.project.id
    assert (
        second.project.local_root == str(moved.resolve())
        or second.project.local_root != first.project.local_root
    )


async def test_different_projects_do_not_share_identity(env: Env) -> None:
    a = await env.indexer.bootstrap(make_repo(env.root, "a", {"a.py": "x=1\n"}))
    b = await env.indexer.bootstrap(make_repo(env.root, "b", {"b.py": "y=2\n"}))
    assert a.project and b.project and a.project.id != b.project.id


# ---------------------------------------------------------------------------- inventory + graph
async def test_initial_index_inventory_symbols_and_graph(env: Env) -> None:
    repo = make_repo(env.root, "proj", MINI_PROJECT)
    pid = (await env.indexer.bootstrap(repo)).project.id  # type: ignore[union-attr]
    result = await env.indexer.sync(pid)
    assert set(result.changed) == set(MINI_PROJECT) and result.overlay == []

    files = {f.path: f for f in await env.store.list_files(pid, "main")}
    assert files["app/service.py"].language == "python" and files["db/schema.sql"].language == "sql"
    assert (
        files["assets/logo.bin"].language == "binary"
        and files["assets/logo.bin"].metadata["skipped"] == "binary"
    )
    assert files["app/service.py"].scope == "committed"

    syms = {s.qualified_name: s.kind for s in await env.store.search_symbols(pid, "main", "user")}
    assert (
        syms["User"] == "class"
        and syms["UserService"] == "class"
        and syms["usp_get_user"] == "procedure"
    )
    assert syms["users"] == "table"
    svc_syms = {s.qualified_name for s in await env.store.file_symbols(files["app/service.py"].id)}
    assert {"UserService", "UserService.create", "UserService.rename"} <= svc_syms

    # imports: file -> file (resolved), unresolved third-party -> external
    imports = await _keys(
        env,
        pid,
        "imports",
        srcs=["file:app/service.py", "file:app/api.py", "file:tests/test_service.py"],
    )
    assert ("file:app/service.py", "file:app/models.py") in imports
    assert ("file:app/api.py", "file:app/service.py") in imports
    assert ("file:app/api.py", "external:requests") in imports
    assert ("file:tests/test_service.py", "file:app/service.py") in imports

    # containment: dir -> file, file -> symbol, class -> method
    contains = await _keys(
        env,
        pid,
        "contains",
        srcs=["dir:app", "file:app/service.py", "symbol:app/service.py::UserService"],
    )
    assert ("dir:app", "file:app/service.py") in contains
    assert ("file:app/service.py", "symbol:app/service.py::UserService") in contains
    assert (
        "symbol:app/service.py::UserService",
        "symbol:app/service.py::UserService.create",
    ) in contains

    # calls resolved across files via the import graph; SQL references resolved by name
    calls = await _keys(
        env,
        pid,
        "calls",
        srcs=["symbol:app/service.py::UserService.create", "symbol:app/api.py::handler"],
    )
    assert ("symbol:app/service.py::UserService.create", "symbol:app/models.py::make_user") in calls
    refs = await _keys(env, pid, "references", srcs=["symbol:db/schema.sql::usp_get_user"])
    assert ("symbol:db/schema.sql::usp_get_user", "symbol:db/schema.sql::users") in refs


# ---------------------------------------------------------------------------- incremental
async def test_only_changed_files_are_reprocessed(env: Env) -> None:
    repo = make_repo(env.root, "proj", MINI_PROJECT)
    pid = (await env.indexer.bootstrap(repo)).project.id  # type: ignore[union-attr]
    await env.indexer.sync(pid)
    assert len(env.registry.parsed) >= 6
    env.reset_counter()

    noop = await env.indexer.sync(pid)  # nothing changed
    assert noop.changed == [] and noop.removed == [] and env.registry.parsed == []

    models_src = str(MINI_PROJECT["app/models.py"])
    write(repo, {"app/models.py": models_src + "\ndef another():\n    return 1\n"})
    commit_all(repo, "edit one file")
    env.reset_counter()
    result = await env.indexer.sync(pid)
    assert result.changed == ["app/models.py"] and result.removed == []
    assert env.registry.parsed == ["app/models.py"]  # exactly one file parsed
    names = {s.name for s in await env.store.search_symbols(pid, "main", "another")}
    assert "another" in names
    # untouched files keep their rows (and their edges)
    imports = await _keys(env, pid, "imports", srcs=["file:app/service.py"])
    assert ("file:app/service.py", "file:app/models.py") in imports


async def test_added_and_removed_files_re_resolve_dependents_without_reparsing_them(
    env: Env,
) -> None:
    repo = make_repo(
        env.root,
        "proj",
        {
            "main.py": "from helper import assist\n\ndef run():\n    assist()\n",
            "readme.md": "# x\n",
        },
    )
    pid = (await env.indexer.bootstrap(repo)).project.id  # type: ignore[union-attr]
    await env.indexer.sync(pid)
    before = await _keys(env, pid, srcs=["file:main.py", "symbol:main.py::run"])
    assert ("file:main.py", "external:helper") in before  # helper.py does not exist yet
    assert ("symbol:main.py::run", "callee:assist") in before

    write(repo, {"helper.py": "def assist():\n    return 1\n"})
    commit_all(repo, "add helper")
    env.reset_counter()
    await env.indexer.sync(pid)
    assert env.registry.parsed == ["helper.py"]  # main.py was NOT re-parsed...
    after = await _keys(env, pid, srcs=["file:main.py", "symbol:main.py::run"])
    assert ("file:main.py", "file:helper.py") in after  # ...yet its import now resolves
    assert ("file:main.py", "external:helper") not in after
    assert ("symbol:main.py::run", "symbol:helper.py::assist") in after

    git(repo, "rm", "-q", "helper.py")
    commit_all(repo, "remove helper")
    env.reset_counter()
    result = await env.indexer.sync(pid)
    assert result.removed == ["helper.py"] and env.registry.parsed == []
    gone = await _keys(env, pid, srcs=["file:main.py", "symbol:main.py::run"])
    assert ("file:main.py", "file:helper.py") not in gone
    assert ("file:main.py", "external:helper") in gone
    assert ("symbol:main.py::run", "callee:assist") in gone
    assert await env.store.search_symbols(pid, "main", "assist") == []


async def test_renamed_function_unresolves_old_callers(env: Env) -> None:
    repo = make_repo(
        env.root,
        "proj",
        {
            "lib.py": "def old_name():\n    return 1\n",
            "app.py": "from lib import old_name\n\ndef go():\n    old_name()\n",
        },
    )
    pid = (await env.indexer.bootstrap(repo)).project.id  # type: ignore[union-attr]
    await env.indexer.sync(pid)
    assert ("symbol:app.py::go", "symbol:lib.py::old_name") in await _keys(
        env, pid, "calls", srcs=["symbol:app.py::go"]
    )
    write(repo, {"lib.py": "def new_name():\n    return 1\n"})
    commit_all(repo, "rename")
    env.reset_counter()
    await env.indexer.sync(pid)
    assert env.registry.parsed == ["lib.py"]
    calls = await _keys(env, pid, "calls", srcs=["symbol:app.py::go"])
    assert ("symbol:app.py::go", "callee:old_name") in calls
    assert ("symbol:app.py::go", "symbol:lib.py::old_name") not in calls


# ---------------------------------------------------------------------------- overlays + branches
async def test_uncommitted_work_lives_in_a_temporary_overlay(env: Env) -> None:
    repo = make_repo(env.root, "proj", MINI_PROJECT)
    pid = (await env.indexer.bootstrap(repo)).project.id  # type: ignore[union-attr]
    await env.indexer.sync(pid)

    service_src = str(MINI_PROJECT["app/service.py"])
    write(
        repo,
        {
            "app/service.py": service_src + "\n    def dirty_method(self):\n        return 1\n",
            "scratch.py": "def untracked_helper():\n    return 2\n",
        },
    )
    (repo / "README.md").unlink()
    result = await env.indexer.sync(pid)
    assert result.state is BootstrapState.DIRTY
    assert result.overlay == ["app/service.py", "scratch.py"] and result.changed == []

    committed = {s.name for s in await env.store.search_symbols(pid, "main", "dirty_method")}
    assert committed == set()  # permanent index is untouched by uncommitted work
    both = (FileScope.COMMITTED, FileScope.OVERLAY)
    overlay = await env.store.search_symbols(pid, "main", "dirty_method", scopes=both)
    assert [(s.scope, s.path) for s in overlay] == [("overlay", "app/service.py")]
    assert [
        s.scope
        for s in await env.store.search_symbols(pid, "main", "untracked_helper", scopes=both)
    ] == ["overlay"]

    overlay_files = await env.store.list_files(pid, "main", scope=FileScope.OVERLAY)
    by_path = {f.path: f for f in overlay_files}
    assert by_path["README.md"].status == "deleted"  # tombstone hides the committed file
    assert all(f.indexed_at for f in overlay_files)
    committed_files = {f.path for f in await env.store.list_files(pid, "main")}
    assert "scratch.py" not in committed_files and "README.md" in committed_files

    # overlay edges resolve against committed + overlay, and never leak into the committed graph
    overlay_calls = await env.store.edges_from(
        pid, "main", ["symbol:scratch.py::untracked_helper"], scopes=both
    )
    assert overlay_calls == [] or all(e["scope"] == "overlay" for e in overlay_calls)
    assert await env.store.edges_from(pid, "main", ["file:scratch.py"]) == []

    # commit the work: the overlay disappears and the content becomes committed
    commit_all(repo, "land it")
    landed = await env.indexer.sync(pid)
    assert landed.state is BootstrapState.CURRENT and landed.overlay == []
    assert await env.store.list_files(pid, "main", scope=FileScope.OVERLAY) == []
    assert {s.name for s in await env.store.search_symbols(pid, "main", "dirty_method")} == {
        "dirty_method"
    }


async def test_reverting_changes_clears_the_overlay(env: Env) -> None:
    repo = make_repo(env.root, "proj", MINI_PROJECT)
    pid = (await env.indexer.bootstrap(repo)).project.id  # type: ignore[union-attr]
    await env.indexer.sync(pid)
    write(repo, {"app/api.py": "def changed():\n    pass\n"})
    assert (await env.indexer.sync(pid)).overlay == ["app/api.py"]
    git(repo, "checkout", "--", "app/api.py")
    assert (await env.indexer.sync(pid)).overlay == []


async def test_branches_never_mix(env: Env) -> None:
    repo = make_repo(env.root, "proj", MINI_PROJECT)
    pid = (await env.indexer.bootstrap(repo)).project.id  # type: ignore[union-attr]
    await env.indexer.sync(pid)
    main_files = {f.path for f in await env.store.list_files(pid, "main")}

    git(repo, "checkout", "-q", "-b", "feature")
    write(repo, {"feature_only.py": "def feature_fn():\n    return 1\n"})
    (repo / "app" / "api.py").unlink()
    commit_all(repo, "feature work")
    write(repo, {"wip.py": "def wip_fn():\n    return 1\n"})  # uncommitted on feature
    await env.indexer.sync(pid)

    feature_files = {f.path for f in await env.store.list_files(pid, "feature")}
    assert "feature_only.py" in feature_files and "app/api.py" not in feature_files
    assert {f.path for f in await env.store.list_files(pid, "main")} == main_files  # main untouched
    assert await env.store.search_symbols(pid, "main", "feature_fn") == []
    both = (FileScope.COMMITTED, FileScope.OVERLAY)
    assert (
        await env.store.search_symbols(pid, "main", "wip_fn", scopes=both) == []
    )  # overlay is feature's
    assert len(await env.store.search_symbols(pid, "feature", "wip_fn", scopes=both)) == 1
    assert await env.store.edges_from(pid, "main", ["file:feature_only.py"]) == []

    git(repo, "checkout", "-q", "main")
    git(repo, "clean", "-fdq")
    back = await env.indexer.sync(pid)
    assert back.branch == "main" and back.changed == [] and back.overlay == []
    assert {f.path for f in await env.store.list_files(pid, "feature")} == feature_files  # kept


async def test_overlay_expiry_purge(env: Env) -> None:
    from datetime import timedelta

    from eios_domain.ids import utcnow

    repo = make_repo(env.root, "proj", MINI_PROJECT)
    pid = (await env.indexer.bootstrap(repo)).project.id  # type: ignore[union-attr]
    await env.indexer.sync(pid)
    write(repo, {"tmp.py": "def t():\n    pass\n"})
    await env.indexer.sync(pid)
    assert await env.store.purge_expired_overlays() == 0
    assert await env.store.purge_expired_overlays(now=utcnow() + timedelta(days=2)) == 1
    both = (FileScope.COMMITTED, FileScope.OVERLAY)
    assert await env.store.list_files(pid, "main", scope=FileScope.OVERLAY) == []
    assert await env.store.edges_from(pid, "main", ["file:tmp.py"], scopes=both) == []


# ---------------------------------------------------------------------------- runs, jobs, coverage
async def test_sync_is_observable_and_queued_syncs_are_deduplicated(env: Env) -> None:
    from eios_jobs import JobRunner

    repo = make_repo(env.root, "proj", MINI_PROJECT)
    pid = (await env.indexer.bootstrap(repo)).project.id  # type: ignore[union-attr]

    job1, run1 = await env.service.request_sync(pid)
    job2, run2 = await env.service.request_sync(pid)
    assert job1.id == job2.id and run1 == run2  # deduplicated while active

    runner = JobRunner(env.container.queue, {"project.sync": env.service.handle_sync_job})
    assert await runner.run_once()
    done = await env.container.queue.get(job1.id)
    assert done is not None and done.status == "succeeded" and done.result is not None
    assert done.result["changed"] == len(MINI_PROJECT)

    events = (await env.container.events.list_events(run1, limit=1000)).items
    types = [e.type for e in events]
    assert types[0] == EventType.RUN_STARTED and types[-1] == EventType.RUN_COMPLETED
    assert types.count(EventType.FILE_CHANGED) == len(MINI_PROJECT)
    synced = next(e for e in events if e.type == EventType.PROJECT_SYNCED)
    assert synced.data["branch"] == "main" and synced.data["changed"] == len(MINI_PROJECT)

    job3, _ = await env.service.request_sync(pid)  # nothing active now -> a new job
    assert job3.id != job1.id


async def test_sync_failure_marks_the_run_failed_when_attempts_are_exhausted(env: Env) -> None:
    from eios_domain.run import RunStatus
    from eios_jobs import JobRunner

    repo = make_repo(env.root, "proj", MINI_PROJECT)
    pid = (await env.indexer.bootstrap(repo)).project.id  # type: ignore[union-attr]
    job, run_id = await env.service.request_sync(pid)
    shutil.rmtree(repo)  # the project vanishes before the worker runs
    runner = JobRunner(
        env.container.queue, {"project.sync": env.service.handle_sync_job}, base_retry_seconds=0
    )
    for _ in range(4):
        await runner.run_once()
    dead = await env.container.queue.get(job.id)
    assert dead is not None and dead.status == "dead"
    run = await env.container.runs.get(run_id)
    assert run is not None and run.status is RunStatus.FAILED


async def test_semantic_coverage_foundation(env: Env) -> None:
    from eios_domain.knowledge import KnowledgeItemCreate, SourceKind
    from eios_domain.vault import Vault

    repo = make_repo(env.root, "proj", MINI_PROJECT)
    pid = (await env.indexer.bootstrap(repo)).project.id  # type: ignore[union-attr]
    await env.indexer.sync(pid)
    cov = await env.service.semantic_coverage(pid)
    assert cov["files_total"] == len(MINI_PROJECT) and cov["files_with_knowledge"] == 0
    assert cov["languages"]["python"] == 5 and cov["symbol_coverage"] > 0.8
    await env.container.knowledge.add_item(
        KnowledgeItemCreate(
            vault=Vault.PROJECT,
            project_id=pid,
            title="service notes",
            content="creates users",
            source_kind=SourceKind.MANUAL,
            created_by="t",
            metadata={"path": "app/service.py"},
        )
    )
    after = await env.service.semantic_coverage(pid)
    assert after["files_with_knowledge"] == 1 and after["knowledge_coverage"] > 0


async def test_neighborhood_for_incremental_graph_loading(env: Env) -> None:
    repo = make_repo(env.root, "proj", MINI_PROJECT)
    pid = (await env.indexer.bootstrap(repo)).project.id  # type: ignore[union-attr]
    await env.indexer.sync(pid)
    hood = await env.service.neighborhood(pid, "file:app/service.py")
    keys = {n["key"] for n in hood["nodes"]}
    assert {"file:app/service.py", "file:app/models.py", "file:app/api.py", "dir:app"} <= keys
    assert not hood["truncated"]
    small = await env.service.neighborhood(pid, "file:app/service.py", limit=1)
    assert small["truncated"]
