"""pgvector query tuning shared by every vector search."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

# HNSW is an approximate index and our queries are filtered (vault/project/health) *after* the
# index scan. With the defaults (ef_search=40) a selective filter can return fewer rows than
# exist, and the result then depends on how the graph happened to be built. Iterative scans keep
# going until enough rows pass the filter, which makes filtered search complete and repeatable.
_EF_SEARCH = "400"
_ITERATIVE = "relaxed_order"
_MAX_SCAN_TUPLES = "50000"


async def tune_vector_search(conn: AsyncConnection) -> None:
    """Transaction-local settings (``set_config(..., true)``); safe if pgvector is older."""
    for name, value in (
        ("hnsw.ef_search", _EF_SEARCH),
        ("hnsw.iterative_scan", _ITERATIVE),
        ("hnsw.max_scan_tuples", _MAX_SCAN_TUPLES),
    ):
        await conn.execute(sa.select(sa.func.set_config(name, value, True)))
