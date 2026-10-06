"""Hardened, read-only git access.

Threat model: the repositories we index may be untrusted. Git can execute repository-defined
programs (``core.fsmonitor``, ``filter.*.clean``/``smudge``, ``diff.*.textconv``, hooks, pagers). We
therefore

* only run an allowlist of read-only plumbing commands that do not apply content filters
  (``rev-parse``, ``ls-tree``, ``cat-file``, ``ls-files -o``, ``rev-list``, ``merge-base``,
  ``diff --name-only --no-ext-diff --no-textconv``, ``remote get-url``);
* never run ``git status`` / ``ls-files -m`` / ``diff-files`` (they can run clean filters) -
  working-tree changes are detected by hashing file content ourselves;
* override the dangerous config keys on the command line and ignore system/global config;
* disable prompts, optional locks and pagers, enforce a timeout and an output cap.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


class GitError(Exception):
    pass


_ALLOWED_COMMANDS = frozenset(
    {
        "rev-parse",
        "ls-tree",
        "cat-file",
        "ls-files",
        "rev-list",
        "merge-base",
        "diff",
        "remote",
        "log",
    }
)
_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_MAX_OUTPUT = 512 * 1024 * 1024


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    author: str
    date: str
    subject: str


@dataclass(frozen=True)
class TreeEntry:
    path: str
    sha: str
    mode: str
    size: int


def git_blob_sha(data: bytes) -> str:
    """The SHA-1 git would assign to ``data`` as a blob (no filters applied)."""
    return hashlib.sha1(  # noqa: S324 - git's object id, not a security hash
        b"blob %d\0" % len(data) + data
    ).hexdigest()


def matches_blob(data: bytes, expected_sha: str) -> bool:
    """True if ``data`` equals the blob, also tolerating CRLF working-tree conversion."""
    if git_blob_sha(data) == expected_sha:
        return True
    return b"\r\n" in data and git_blob_sha(data.replace(b"\r\n", b"\n")) == expected_sha


class SubprocessGit:
    def __init__(self, root: Path, *, timeout: float = 60.0, git_binary: str | None = None) -> None:
        self._root = root
        self._timeout = timeout
        self._git = git_binary or shutil.which("git")
        if self._git is None:
            raise GitError("git executable not found")

    def _command(self, args: tuple[str, ...]) -> list[str]:
        if not args or args[0] not in _ALLOWED_COMMANDS:
            raise GitError(f"git command not allowed: {args[:1]}")
        if self._git is None:
            raise GitError("git executable not found")
        return [
            self._git,
            "-c", "core.fsmonitor=false",
            "-c", "core.untrackedCache=false",
            "-c", "core.pager=cat",
            "-c", "core.hooksPath=",
            "-c", "diff.external=",
            "-c", f"safe.directory={self._root}",
            "-C", str(self._root),
            *args,
        ]  # fmt: skip

    @staticmethod
    def _env() -> dict[str, str]:
        env = {
            k: v
            for k, v in os.environ.items()
            if k in {"PATH", "SYSTEMROOT", "TMP", "TEMP", "TMPDIR", "LANG"}
        }
        env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
            GIT_OPTIONAL_LOCKS="0",
            GIT_PAGER="cat",
            GIT_EXTERNAL_DIFF="",
            LC_ALL="C",
        )
        return env

    async def _run(self, *args: str, stdin: bytes | None = None) -> bytes:
        proc = await asyncio.create_subprocess_exec(
            *self._command(args),
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env(),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), self._timeout)
        except TimeoutError as exc:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise GitError(f"git {args[0]} timed out") from exc
        if proc.returncode != 0:
            raise GitError(f"git {args[0]} failed: {err.decode(errors='replace').strip()[:300]}")
        if len(out) > _MAX_OUTPUT:
            raise GitError("git output too large")
        return out

    async def toplevel(self) -> Path:
        return Path((await self._run("rev-parse", "--show-toplevel")).decode().strip())

    async def head(self) -> str:
        sha = (await self._run("rev-parse", "--verify", "HEAD")).decode().strip()
        if not _SHA.match(sha):
            raise GitError("unexpected HEAD value")
        return sha

    async def branch(self) -> str:
        """Current branch name, or ``detached/<sha12>`` for a detached HEAD."""
        name = (await self._run("rev-parse", "--abbrev-ref", "HEAD")).decode().strip()
        if name == "HEAD":
            return f"detached/{(await self.head())[:12]}"
        return name

    async def root_commit(self) -> str | None:
        out = (await self._run("rev-list", "--max-parents=0", "HEAD")).decode().split()
        return sorted(out)[0] if out else None

    async def remote_url(self) -> str | None:
        try:
            out = await self._run("remote", "get-url", "origin")
        except GitError:
            return None
        return out.decode().strip() or None

    async def tree(self) -> dict[str, TreeEntry]:
        """Tracked blobs at HEAD (symlinks and submodules excluded)."""
        out = await self._run("ls-tree", "-r", "-z", "-l", "--full-tree", "HEAD")
        entries: dict[str, TreeEntry] = {}
        for record in out.split(b"\0"):
            if not record:
                continue
            meta, _, path = record.partition(b"\t")
            mode, kind, sha, size = meta.decode().split()
            if kind != "blob" or mode in {"120000", "160000"}:
                continue
            decoded = path.decode("utf-8", errors="surrogateescape")
            entries[decoded] = TreeEntry(decoded, sha, mode, 0 if size == "-" else int(size))
        return entries

    async def untracked(self, *, limit: int = 5000) -> tuple[list[str], bool]:
        """Untracked, non-ignored files (``.gitignore`` is honoured). Returns (paths, truncated)."""
        out = await self._run("ls-files", "-o", "--exclude-standard", "-z")
        paths = [p.decode("utf-8", errors="surrogateescape") for p in out.split(b"\0") if p]
        return paths[:limit], len(paths) > limit

    async def read_blobs(self, shas: list[str], *, max_bytes: int) -> dict[str, bytes | None]:
        """Read blob contents via ``cat-file --batch``. Blobs over ``max_bytes`` map to ``None``."""
        if not shas:
            return {}
        for sha in shas:
            if not _SHA.match(sha):
                raise GitError("invalid object id")
        out = await self._run("cat-file", "--batch", stdin=("\n".join(shas) + "\n").encode())
        result: dict[str, bytes | None] = {}
        pos = 0
        for sha in shas:
            end = out.index(b"\n", pos)
            header = out[pos:end].decode().split()
            pos = end + 1
            if len(header) != 3 or header[1] != "blob":
                result[sha] = None
                continue
            size = int(header[2])
            data = out[pos : pos + size]
            pos += size + 1
            result[sha] = data if size <= max_bytes else None
        return result

    async def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        if not (_SHA.match(ancestor) and _SHA.match(descendant)):
            raise GitError("invalid object id")
        try:
            await self._run("merge-base", "--is-ancestor", ancestor, descendant)
        except GitError:
            return False
        return True

    async def has_object(self, sha: str) -> bool:
        if not _SHA.match(sha):
            return False
        try:
            await self._run("cat-file", "-e", f"{sha}^{{commit}}")
        except GitError:
            return False
        return True

    async def log(
        self, *, paths: list[str] | None = None, grep: str | None = None, limit: int = 20
    ) -> list[CommitInfo]:
        """Commit headers (no diffs, so no textconv/external diff can run), newest first.

        ``grep`` is passed as ``--grep=<text>`` (fixed-string, case-insensitive) so it can never be
        parsed as an option; paths follow a ``--`` separator.
        """
        args = [
            "log",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            f"--max-count={max(1, min(limit, 200))}",
            "--format=%H%x1f%an%x1f%aI%x1f%s%x1e",
        ]
        if grep:
            args += [f"--grep={grep[:200]}", "--fixed-strings", "--regexp-ignore-case"]
        if paths:
            args += ["--", *[p for p in paths if (p and not p.startswith("-")) or "/" in p][:50]]
        out = (await self._run(*args)).decode("utf-8", errors="replace")
        commits: list[CommitInfo] = []
        for record in out.split("\x1e"):
            fields = record.strip("\n").split("\x1f")
            if len(fields) == 4 and _SHA.match(fields[0]):
                commits.append(CommitInfo(*fields))
        return commits

    async def changed_paths(self, old: str, new: str) -> list[str]:
        if not (_SHA.match(old) and _SHA.match(new)):
            raise GitError("invalid object id")
        out = await self._run(
            "diff", "--name-only", "--no-ext-diff", "--no-textconv", "-z", old, new
        )
        return [p.decode("utf-8", errors="surrogateescape") for p in out.split(b"\0") if p]
