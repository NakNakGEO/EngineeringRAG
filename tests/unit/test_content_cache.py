"""The hash memory that lets an unchanged file go unread (and its racy-file guard)."""

from __future__ import annotations

import os
import time
from pathlib import Path

from eios_project_intelligence.content_cache import RACY_NS, ContentFingerprints
from eios_project_intelligence.index_version import index_version


def _old_file(
    tmp_path: Path, name: str = "a.txt", text: str = "hello"
) -> tuple[Path, os.stat_result]:
    p = tmp_path / name
    p.write_text(text)
    past = time.time() - 60  # well outside the racy window
    os.utime(p, (past, past))
    return p, os.lstat(p)


def test_an_unchanged_file_is_remembered(tmp_path: Path) -> None:
    cache = ContentFingerprints()
    p, st = _old_file(tmp_path)
    assert cache.get(str(tmp_path), "a.txt", st) is None
    cache.put(str(tmp_path), "a.txt", st, "sha-1", "head-sha")
    got = cache.get(str(tmp_path), "a.txt", os.lstat(p))
    assert got is not None and got.blob_sha == "sha-1" and got.clean_sha == "head-sha"


def test_a_changed_size_or_mtime_is_not_trusted(tmp_path: Path) -> None:
    cache = ContentFingerprints()
    p, st = _old_file(tmp_path)
    cache.put(str(tmp_path), "a.txt", st, "sha-1")
    newer = time.time() - 30
    os.utime(p, (newer, newer))  # same size, new mtime
    assert cache.get(str(tmp_path), "a.txt", os.lstat(p)) is None
    p.write_text("hello, longer now")
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))  # the old mtime back, but another size
    assert cache.get(str(tmp_path), "a.txt", os.lstat(p)) is None


def test_a_file_written_just_before_it_was_hashed_is_not_trusted(tmp_path: Path) -> None:
    cache = ContentFingerprints()
    p = tmp_path / "fresh.txt"
    p.write_text("x")  # mtime is now: inside the racy window when hashed right away
    cache.put(str(tmp_path), "fresh.txt", os.lstat(p), "sha")
    assert cache.get(str(tmp_path), "fresh.txt", os.lstat(p)) is None
    assert RACY_NS >= 1_000_000_000


def test_the_cache_never_grows_without_bound(tmp_path: Path) -> None:
    cache = ContentFingerprints(max_entries=3)
    _, st = _old_file(tmp_path)
    for n in range(10):
        cache.put(str(tmp_path), f"f{n}", st, "s")
    assert len(cache) <= 3


def test_the_index_version_is_a_stable_stamp() -> None:
    assert index_version() == index_version() and len(index_version()) == 12
