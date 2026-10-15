"""Helpers that build real git repositories for tests (no network, no global config)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_TERMINAL_PROMPT": "0",
}


def git(repo: Path, *args: str, check: bool = True) -> str:
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG"}} | _ENV
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],  # noqa: S607
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {args} failed: {result.stderr}")
    return result.stdout.strip()


def write(repo: Path, files: dict[str, str | bytes]) -> None:
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")


def make_repo(
    root: Path,
    name: str,
    files: dict[str, str | bytes],
    *,
    branch: str = "main",
    remote: str | None = None,
) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", branch)
    if remote:
        git(repo, "remote", "add", "origin", remote)
    write(repo, files)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    return repo


def commit_all(repo: Path, message: str = "change") -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


MINI_PROJECT: dict[str, str | bytes] = {
    "app/__init__.py": "",
    "app/models.py": (
        "class User:\n    def __init__(self, name):\n        self.name = name\n\n"
        "def make_user(name):\n    return User(name)\n"
    ),
    "app/service.py": (
        "from app.models import User, make_user\n\n"
        "class UserService:\n    def create(self, name):\n        return make_user(name)\n\n"
        "    def rename(self, user, name):\n        user.name = name\n        return user\n"
    ),
    "app/api.py": (
        "from app.service import UserService\nimport requests\n\n"
        "def handler(name):\n    svc = UserService()\n    return svc.create(name)\n"
    ),
    "tests/test_service.py": (
        "from app.service import UserService\n\n"
        "def test_create():\n    assert UserService().create('a').name == 'a'\n"
    ),
    "db/schema.sql": (
        "CREATE TABLE users (id INT PRIMARY KEY, name TEXT);\n"
        "GO\nCREATE PROCEDURE usp_get_user @id INT AS\nBEGIN\n"
        "    SELECT * FROM users WHERE id = @id;\nEND\n"
    ),
    "README.md": "# Mini\n\nA tiny project.\n\n## Usage\n\nCall the handler.\n",
    "assets/logo.bin": b"\x00\x01\x02binary\x00",
}
