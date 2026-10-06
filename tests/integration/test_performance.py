"""Performance budgets. Thresholds are deliberately generous: they catch order-of-magnitude
regressions (a missing index, an N+1) without flaking on a slow CI runner."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.knowledge import KnowledgeItemCreate, SourceKind
from eios_domain.vault import Vault
from eios_knowledge.scope import SearchScope
from eios_runtime import build_container
from tests.conftest import make_settings

pytestmark = [pytest.mark.integration, pytest.mark.perf]


async def test_event_append_is_gap_free_and_fast_under_concurrency(
    db: AsyncEngine, tmp_path: Path
) -> None:
    c = build_container(make_settings(blob_dir=tmp_path), db)
    ctx = await c.recorder.start_run(kind="perf", goal="append")
    writers, per_writer = 8, 150

    async def writer(n: int) -> None:
        for i in range(per_writer):
            await ctx.emit("TOOL_STARTED", f"w{n} e{i}")  # type: ignore[arg-type]

    started = time.monotonic()
    await asyncio.gather(*(writer(n) for n in range(writers)))
    elapsed = time.monotonic() - started
    seqs = []
    after = 0
    while True:
        page = await c.events.list_events(ctx.run_id, after_seq=after, limit=1000)
        seqs += [e.seq for e in page.items]
        if not page.has_more:
            break
        after = page.items[-1].seq
    assert seqs == list(range(1, len(seqs) + 1)) and len(seqs) == writers * per_writer + 1
    assert elapsed < 30, f"{writers * per_writer} appends took {elapsed:.1f}s"


async def test_hybrid_search_over_thousands_of_items_is_interactive(
    db: AsyncEngine, tmp_path: Path
) -> None:
    c = build_container(make_settings(blob_dir=tmp_path), db)
    topics = ["retry", "ledger", "token", "schema", "cache", "queue", "audit", "index"]
    for i in range(1500):
        await c.knowledge.add_item(
            KnowledgeItemCreate(
                vault=Vault.DEFAULT,
                title=f"{topics[i % 8]} note {i}",
                content=(
                    f"{topics[i % 8]} handling number {i} with detail {i * 7} "
                    f"about {topics[(i + 3) % 8]}"
                ),
                source_kind=SourceKind.MANUAL,
                created_by="perf",
            )
        )
    scope = SearchScope(vaults=frozenset({Vault.DEFAULT}))
    timings = []
    for q in ("retry handling", "ledger audit", "retry schema", "token queue"):
        t0 = time.monotonic()
        text = await c.knowledge.search_text(q, scope, limit=20)
        vec = await c.knowledge.search_vector(q, scope, limit=20)
        timings.append(time.monotonic() - t0)
        assert text, ("fts empty", q)
        assert vec, ("vector empty", q)
    assert max(timings) < 2.0, f"slowest hybrid search {max(timings):.2f}s"
