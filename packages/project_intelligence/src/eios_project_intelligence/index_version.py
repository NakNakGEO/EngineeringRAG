"""A stamp that changes whenever the code that turns a file into index rows changes.

Rows written for a file are only reused for the same content when they were produced by the same
parsers, chunker and graph builder; otherwise they are rebuilt once. The stamp is a hash of those
modules' source, so nobody has to remember to bump a constant.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

_FILES = ("chunking.py", "languages.py", "graph_builder.py", "resolver.py")


@lru_cache(maxsize=1)
def index_version() -> str:
    here = Path(__file__).parent
    digest = hashlib.sha1(usedforsecurity=False)
    names = [
        *_FILES,
        *sorted(p.relative_to(here).as_posix() for p in (here / "parsers").glob("*.py")),
    ]
    for name in names:
        try:
            digest.update(name.encode())
            digest.update((here / name).read_bytes())
        except OSError:  # a missing source file (zipped install): fall back to a fixed stamp
            digest.update(b"unreadable")
    return digest.hexdigest()[:12]
