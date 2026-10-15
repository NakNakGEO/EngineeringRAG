"""Remember what a file's content hashed to, as of its size and modification time.

Telling uncommitted work from the last commit means hashing every file of the working tree, and
on a network or Windows-mounted drive reading the files is the slowest part of a sync. A file
whose size and mtime are unchanged since it was hashed has the same content: the assumption
git's own index makes, with git's guard against files written within the clock's resolution of
being hashed ("racy" entries are never trusted until they are hashed again).

In-process only. A restart starts cold, which is slower but never wrong.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

RACY_NS = 2_000_000_000  # a file this fresh when hashed is hashed again on the next sync
MAX_ENTRIES = 250_000


@dataclass(frozen=True, slots=True)
class Fingerprint:
    mtime_ns: int
    size: int
    blob_sha: str  # git blob hash of the bytes as they are on disk
    clean_sha: str | None  # tracked file: the HEAD blob these bytes were found to match
    verified_ns: int  # wall-clock time of the hashing


class ContentFingerprints:
    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self._entries: dict[tuple[str, str], Fingerprint] = {}
        self._max = max_entries
        self._lock = threading.Lock()

    def get(self, root: str, rel: str, st: os.stat_result) -> Fingerprint | None:
        """The remembered hash if the file still has the size and mtime it was hashed at."""
        with self._lock:
            fp = self._entries.get((root, rel))
        if fp is None or fp.mtime_ns != st.st_mtime_ns or fp.size != st.st_size:
            return None
        if fp.verified_ns - st.st_mtime_ns < RACY_NS:
            return None
        return fp

    def put(
        self, root: str, rel: str, st: os.stat_result, blob_sha: str, clean_sha: str | None = None
    ) -> None:
        with self._lock:
            if len(self._entries) >= self._max:
                self._entries.clear()
            self._entries[(root, rel)] = Fingerprint(
                st.st_mtime_ns, st.st_size, blob_sha, clean_sha, time.time_ns()
            )

    def __len__(self) -> int:
        return len(self._entries)
