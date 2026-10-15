"""Indexing untrusted repositories must never execute repository-defined programs or read files
outside the workspace."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.project import FileScope
from eios_project_intelligence import ApprovedWorkspaces, ProjectIndexer, ProjectStore
from eios_project_intelligence.git import GitError, SubprocessGit
from tests.gitfixtures import commit_all, git, make_repo, write

pytestmark = pytest.mark.security


def _script(path: Path, marker: Path, tail: str = "") -> str:
    path.write_text(f"#!/bin/sh\ntouch '{marker}'\n{tail}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _armed_repo(root: Path, tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    """A repository whose own config tries to run programs on common git operations."""
    markers = {
        "fsmonitor": tmp_path / "MARK_FSMONITOR",
        "clean": tmp_path / "MARK_CLEAN_FILTER",
        "textconv": tmp_path / "MARK_TEXTCONV",
        "hook": tmp_path / "MARK_HOOK",
        "pager": tmp_path / "MARK_PAGER",
    }
    repo = make_repo(
        root,
        "evil",
        {
            ".gitattributes": "*.txt filter=evil diff=evil\n",
            "a.txt": "hello\n",
            "code.py": "def f():\n    pass\n",
        },
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    git(repo, "config", "core.fsmonitor", _script(scripts / "fsm.sh", markers["fsmonitor"]))
    git(repo, "config", "filter.evil.clean", _script(scripts / "clean.sh", markers["clean"], "cat"))
    git(
        repo,
        "config",
        "filter.evil.smudge",
        _script(scripts / "smudge.sh", markers["clean"], "cat"),
    )
    git(
        repo,
        "config",
        "diff.evil.textconv",
        _script(scripts / "tc.sh", markers["textconv"], 'cat "$1"'),
    )
    git(repo, "config", "core.pager", _script(scripts / "pager.sh", markers["pager"], "cat"))
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{markers['hook']}'\n")
    hook.chmod(0o755)
    # make the working tree dirty so any status/diff machinery has reason to run filters
    write(repo, {"a.txt": "hello changed\n", "untracked.txt": "new\n"})
    return repo, markers


def test_control_plain_git_status_really_does_execute_repo_config(tmp_path: Path) -> None:
    """Proves the hostile repository is genuinely dangerous, so the tests below are meaningful."""
    root = tmp_path / "ws"
    root.mkdir()
    repo, markers = _armed_repo(root, tmp_path)
    env = {"PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "status", "--porcelain"],  # noqa: S607
        capture_output=True, env=env, check=False,
    )  # fmt: skip
    assert markers["fsmonitor"].exists() or markers["clean"].exists()


async def test_indexing_a_hostile_repository_executes_nothing(
    db: AsyncEngine, tmp_path: Path
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    repo, markers = _armed_repo(root, tmp_path)
    for marker in markers.values():
        marker.unlink(missing_ok=True)

    store = ProjectStore(db)
    indexer = ProjectIndexer(store, ApprovedWorkspaces([root]))
    boot = await indexer.bootstrap(repo)
    assert boot.project is not None
    result = await indexer.sync(boot.project.id)
    assert (
        "a.txt" in result.overlay and "untracked.txt" in result.overlay
    )  # it did look at the tree
    again = await indexer.bootstrap(repo)
    assert again.project is not None

    executed = [name for name, marker in markers.items() if marker.exists()]
    assert executed == [], f"repository-defined programs were executed: {executed}"


@pytest.mark.parametrize(
    "command",
    [
        "status",
        "config",
        "fetch",
        "push",
        "checkout",
        "clone",
        "gc",
        "reset",
        "add",
        "commit",
        "pull",
        "diff-files",
        "stash",
    ],
)
async def test_only_allowlisted_read_only_git_commands_can_run(
    tmp_path: Path, command: str
) -> None:
    repo = make_repo(tmp_path, "r", {"a.py": "x=1\n"})
    client = SubprocessGit(repo)
    with pytest.raises(GitError, match="not allowed"):
        await client._run(command)


async def test_object_ids_are_validated_before_reaching_git(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "r", {"a.py": "x=1\n"})
    client = SubprocessGit(repo)
    for bad in ("--help", "HEAD; rm -rf /", "../x", "abc", "g" * 40):
        with pytest.raises(GitError):
            await client.read_blobs([bad], max_bytes=10)
        with pytest.raises(GitError):
            await client.changed_paths(bad, bad)
        assert not await client.has_object(bad)
        with pytest.raises(GitError):
            await client.is_ancestor(bad, bad)


async def test_symlinks_pointing_outside_the_workspace_are_never_read(
    db: AsyncEngine, tmp_path: Path
) -> None:
    secret = tmp_path / "outside_secret.py"
    secret.write_text("def leaked_secret_symbol():\n    return 'TOP-SECRET'\n")
    secret_dir = tmp_path / "secret_dir"
    secret_dir.mkdir()
    (secret_dir / "hidden.py").write_text("def leaked_dir_symbol():\n    pass\n")

    root = tmp_path / "ws"
    root.mkdir()
    repo = make_repo(root, "proj", {"ok.py": "def fine():\n    pass\n"})
    (repo / "tracked_link.py").symlink_to(secret)
    (repo / "linked_dir").symlink_to(secret_dir, target_is_directory=True)
    commit_all(repo, "add links")  # tracked symlinks
    (repo / "untracked_link.py").symlink_to(secret)  # and an untracked one

    store = ProjectStore(db)
    indexer = ProjectIndexer(store, ApprovedWorkspaces([root]))
    boot = await indexer.bootstrap(repo)
    assert boot.project
    result = await indexer.sync(boot.project.id)
    both = (FileScope.COMMITTED, FileScope.OVERLAY)
    for name in ("leaked_secret_symbol", "leaked_dir_symbol"):
        assert await store.search_symbols(boot.project.id, "main", name, scopes=both) == []
    files = {f.path for f in await store.list_files(boot.project.id, "main")}
    assert (
        "ok.py" in files and "tracked_link.py" not in files and "linked_dir/hidden.py" not in files
    )
    overlay = {
        f.path: f for f in await store.list_files(boot.project.id, "main", scope=FileScope.OVERLAY)
    }
    if "untracked_link.py" in overlay:  # recorded at most as unreadable, never with content
        assert overlay["untracked_link.py"].metadata.get("skipped") == "unreadable"
        assert overlay["untracked_link.py"].line_count == 0
    assert "untracked_link.py" in result.overlay or "untracked_link.py" not in files


async def test_unusual_filenames_are_indexed_safely(db: AsyncEngine, tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    names = [
        "ünïcode name.py",
        "with space/and tab\there.py",
        "-leading-dash.py",
        "semi;colon&amp.py",
        "$(touch pwned).py",
    ]
    repo = make_repo(root, "proj", {n: f"def f_{i}():\n    pass\n" for i, n in enumerate(names)})
    store = ProjectStore(db)
    indexer = ProjectIndexer(store, ApprovedWorkspaces([root]))
    boot = await indexer.bootstrap(repo)
    assert boot.project
    await indexer.sync(boot.project.id)
    files = {f.path for f in await store.list_files(boot.project.id, "main")}
    assert set(names) <= files
    assert not (repo / "pwned").exists() and not Path("pwned").exists()


async def test_files_outside_the_workspace_cannot_be_indexed_via_project_root_tampering(
    db: AsyncEngine, tmp_path: Path
) -> None:
    """Even if a project's stored root is changed to somewhere unapproved, sync refuses."""
    import sqlalchemy as sa

    from eios_project_intelligence import WorkspaceViolationError
    from eios_storage.tables.project import project as project_t

    root = tmp_path / "ws"
    root.mkdir()
    repo = make_repo(root, "proj", {"a.py": "x=1\n"})
    elsewhere = make_repo(tmp_path, "elsewhere", {"b.py": "y=2\n"})
    store = ProjectStore(db)
    indexer = ProjectIndexer(store, ApprovedWorkspaces([root]))
    boot = await indexer.bootstrap(repo)
    assert boot.project
    async with db.begin() as conn:
        await conn.execute(
            sa.update(project_t)
            .where(project_t.c.id == boot.project.id)
            .values(local_root=str(elsewhere))
        )
    with pytest.raises(WorkspaceViolationError):
        await indexer.sync(boot.project.id)


async def test_log_arguments_cannot_be_smuggled_as_git_options(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "r", {"a.py": "x = 1\n"})
    client = SubprocessGit(repo)
    sentinel = tmp_path / "pwned"
    # a --grep value that looks like an option is still just text (prefixed with --grep=)
    assert await client.log(grep=f"--output={sentinel}") == []
    assert await client.log(grep=f"--exec=touch {sentinel}") == []
    # paths come after "--": option-looking names are only pathspecs
    assert await client.log(paths=[f"--output={sentinel}", "-p"]) == []
    assert not sentinel.exists()
    commits = await client.log(paths=["a.py"], limit=5)
    assert [c.subject for c in commits] == ["initial"] and len(commits[0].sha) == 40
    assert len(await client.log(limit=10_000)) == 1  # the limit is clamped, not trusted
