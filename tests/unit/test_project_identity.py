from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from eios_domain.project import normalize_remote_url, project_fingerprint
from eios_project_intelligence.git import git_blob_sha, matches_blob
from eios_project_intelligence.workspace import ApprovedWorkspaces, WorkspaceViolationError


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://github.com/Org/Repo.git", "github.com/org/repo"),
        ("https://user:token123@GitHub.com/Org/Repo", "github.com/org/repo"),
        ("git@github.com:Org/Repo.git", "github.com/org/repo"),
        ("ssh://git@host.example:2222/team/app.git", "host.example/team/app"),
        ("git@gitlab.corp.local:group/sub/proj", "gitlab.corp.local/group/sub/proj"),
        ("https://github.com/org/repo/", "github.com/org/repo"),
    ],
)
def test_remote_normalisation_strips_noise_and_credentials(raw: str, expected: str) -> None:
    assert normalize_remote_url(raw) == expected
    assert "token123" not in normalize_remote_url(raw)


def test_equivalent_remotes_give_the_same_identity() -> None:
    a = project_fingerprint(remote_url="git@github.com:Org/Repo.git", root_commit="abc", path="/x")
    b = project_fingerprint(
        remote_url="https://tok@github.com/org/repo", root_commit="abc", path="/y"
    )
    assert a == b  # same project in two clones / at two paths


def test_identity_does_not_depend_on_path_when_a_commit_or_remote_is_known() -> None:
    assert project_fingerprint(
        remote_url=None, root_commit="abc", path="/a"
    ) == project_fingerprint(remote_url=None, root_commit="abc", path="/b")
    assert project_fingerprint(
        remote_url="h/o/r", root_commit=None, path="/a"
    ) == project_fingerprint(remote_url="h/o/r", root_commit=None, path="/b")


def test_different_projects_get_different_identities() -> None:
    base = {"remote_url": "github.com/o/r", "path": "/p"}
    assert project_fingerprint(root_commit="1", **base) != project_fingerprint(
        root_commit="2", **base
    )
    assert project_fingerprint(remote_url=None, root_commit=None, path="/a") != project_fingerprint(
        remote_url=None, root_commit=None, path="/b"
    )


def test_git_blob_hash_matches_real_git(tmp_path: Path) -> None:
    env = {"PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    for data in (b"", b"hello\n", b"line1\r\nline2\r\n", bytes(range(256)) * 10):
        f = tmp_path / "f"
        f.write_bytes(data)
        real = subprocess.run(  # noqa: S603
            ["git", "hash-object", "--no-filters", str(f)],  # noqa: S607
            capture_output=True, text=True, env=env, check=True,
        ).stdout.strip()  # fmt: skip
        assert git_blob_sha(data) == real


def test_crlf_working_tree_matches_lf_blob() -> None:
    lf = b"a\nb\n"
    assert matches_blob(b"a\r\nb\r\n", git_blob_sha(lf))
    assert matches_blob(lf, git_blob_sha(lf))
    assert not matches_blob(b"a\r\nB\r\n", git_blob_sha(lf))


def test_workspace_guard(tmp_path: Path) -> None:
    inside = tmp_path / "ws" / "proj"
    inside.mkdir(parents=True)
    outside = tmp_path / "other"
    outside.mkdir()
    guard = ApprovedWorkspaces([tmp_path / "ws"])
    assert guard.resolve(inside) == inside.resolve()
    assert guard.resolve(tmp_path / "ws") == (tmp_path / "ws").resolve()
    for bad in (outside, tmp_path, tmp_path / "ws" / ".." / "other", Path("/etc")):
        with pytest.raises(WorkspaceViolationError):
            guard.resolve(bad)


def test_workspace_guard_resolves_symlinks_before_checking(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    (ws / "link").symlink_to(secret, target_is_directory=True)
    with pytest.raises(WorkspaceViolationError):
        ApprovedWorkspaces([ws]).resolve(ws / "link")


def test_no_roots_means_nothing_is_readable(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceViolationError, match="no approved"):
        ApprovedWorkspaces([]).resolve(tmp_path)


def test_workspace_guard_rejects_files_and_missing_paths(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("x")
    guard = ApprovedWorkspaces([tmp_path])
    with pytest.raises(WorkspaceViolationError, match="directory"):
        guard.resolve(tmp_path / "f.txt")
    with pytest.raises(WorkspaceViolationError):
        guard.resolve(tmp_path / "missing")
