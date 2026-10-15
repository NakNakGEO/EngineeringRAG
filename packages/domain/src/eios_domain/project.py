"""Project identity and bootstrap state (pure)."""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from urllib.parse import urlsplit


class BootstrapState(StrEnum):
    NEW = "NEW"  # never seen before
    CURRENT = "CURRENT"  # same branch, same commit, clean tree
    STALE = "STALE"  # same branch, HEAD moved forward since the last sync
    DIRTY = "DIRTY"  # same commit, uncommitted changes present
    BRANCH_CHANGED = "BRANCH_CHANGED"  # a different branch is checked out
    MAJOR_DIVERGENCE = "MAJOR_DIVERGENCE"  # history rewritten or very large change set
    ERROR = "ERROR"  # git/filesystem problem


class FileScope(StrEnum):
    COMMITTED = "committed"  # content of a commit on a branch
    OVERLAY = "overlay"  # uncommitted working-tree state; temporary, never permanent knowledge


_SCP_LIKE = re.compile(r"^(?:(?P<user>[^@/\s]+)@)?(?P<host>[^:/\s]+):(?P<path>[^\s]+)$")


def normalize_remote_url(url: str) -> str:
    """Canonical ``host/path`` for a git remote: no credentials, scheme, ``.git`` or case noise.

    ``https://user:tok@GitHub.com/Org/Repo.git`` and ``git@github.com:Org/Repo`` both become
    ``github.com/org/repo``.
    """
    url = url.strip()
    if "://" in url:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        path = parts.path
    else:
        match = _SCP_LIKE.match(url)
        if match is None:
            return url.lower().rstrip("/").removesuffix(".git")
        host, path = match.group("host").lower(), match.group("path")
    path = path.strip("/").removesuffix(".git").lower()
    return f"{host}/{path}"


def project_fingerprint(*, remote_url: str | None, root_commit: str | None, path: str) -> str:
    """Stable identity that does not depend on where the clone lives.

    Preference: (remote, root commit) > root commit alone > path (weakest; identity then follows
    the location). A remote or commit-based identity survives moving or re-cloning the project.
    """
    if remote_url and root_commit:
        basis = f"remote+root:{normalize_remote_url(remote_url)}:{root_commit}"
    elif root_commit:
        basis = f"root:{root_commit}"
    elif remote_url:
        basis = f"remote:{normalize_remote_url(remote_url)}"
    else:
        basis = f"path:{path}"
    return hashlib.sha256(basis.encode()).hexdigest()
