"""Approved workspace roots: Engineering OS only reads projects inside directories the owner named.

This is the Phase 3 enforcement point for filesystem scope; the Policy Engine (Phase 6) builds on
the same guard. With no roots configured nothing can be read (secure by default).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


class WorkspaceViolationError(PermissionError):
    """A path outside the approved workspace roots was requested."""


class ApprovedWorkspaces:
    def __init__(self, roots: Sequence[Path]) -> None:
        self._roots = [Path(r).resolve() for r in roots]

    @property
    def roots(self) -> list[Path]:
        return list(self._roots)

    def contains(self, path: Path) -> bool:
        try:
            real = Path(path).resolve()
        except OSError:
            return False
        return any(real == root or real.is_relative_to(root) for root in self._roots)

    def resolve(self, path: str | Path, *, must_be_dir: bool = True) -> Path:
        """Return the real path of ``path`` if it lies inside an approved root.

        Symlinks are resolved *before* the check, so a link pointing out of the workspace is
        rejected. Error messages do not reveal whether an outside path exists.
        """
        if not self._roots:
            raise WorkspaceViolationError("no approved workspace roots are configured")
        try:
            real = Path(path).resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkspaceViolationError("path is not accessible") from exc
        if not self.contains(real):
            raise WorkspaceViolationError("path is outside the approved workspace roots")
        if must_be_dir and not real.is_dir():
            raise WorkspaceViolationError("path is not a directory")
        return real
