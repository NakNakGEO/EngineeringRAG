"""Content-addressed local blob storage (large raw evidence lives on disk, metadata in the DB)."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class BlobError(Exception):
    pass


@dataclass(frozen=True)
class BlobRef:
    sha256: str
    size_bytes: int
    storage_path: str  # relative to the store root, POSIX-style


class BlobStore(Protocol):
    def put(self, data: bytes) -> BlobRef: ...

    def get(self, sha256: str) -> bytes: ...

    def exists(self, sha256: str) -> bool: ...

    def delete(self, sha256: str) -> bool: ...


class LocalBlobStore:
    """Files at ``<root>/<aa>/<bb>/<sha256>``; writes are atomic; reads verify the hash."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, sha256: str) -> Path:
        if not _SHA256.fullmatch(sha256):  # also blocks path traversal
            raise BlobError("invalid blob id")
        return self._root / sha256[:2] / sha256[2:4] / sha256

    def put(self, data: bytes) -> BlobRef:
        digest = hashlib.sha256(data).hexdigest()
        path = self._path(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp)
                raise
        return BlobRef(digest, len(data), path.relative_to(self._root).as_posix())

    def get(self, sha256: str) -> bytes:
        path = self._path(sha256)
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise BlobError("blob not found") from exc
        if hashlib.sha256(data).hexdigest() != sha256:
            raise BlobError("blob is corrupted (hash mismatch)")
        return data

    def exists(self, sha256: str) -> bool:
        return self._path(sha256).is_file()

    def delete(self, sha256: str) -> bool:
        path = self._path(sha256)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True
