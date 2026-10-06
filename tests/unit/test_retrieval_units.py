from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eios_domain.knowledge import Health, Trust
from eios_knowledge.text import or_query, significant_terms
from eios_retrieval.entities import extract_entities
from eios_retrieval.fusion import HeuristicReranker, dedupe, estimate_tokens, rrf_fuse
from eios_retrieval.governor import cost_of, select_within_budget
from eios_retrieval.models import (
    Candidate,
    CandidateKind,
    ContextItem,
    ContextLevel,
    Source,
)


def _k(
    id_: str,
    source: Source = Source.FTS,
    rank: int = 1,
    *,
    title: str = "t",
    content: str = "",
    trust: Trust | None = Trust.DERIVED,
    health: Health | None = Health.CURRENT,
    subject: str | None = None,
    updated: datetime | None = None,
) -> Candidate:
    return Candidate(
        id=id_,
        kind=CandidateKind.KNOWLEDGE,
        title=title,
        content=content,
        source=source,
        rank=rank,
        trust=trust,
        health=health,
        subject_key=subject,
        metadata={"updated_at": (updated or datetime.now(UTC)).isoformat()},
    )


def _item(
    candidate: Candidate,
    score: float = 1.0,
    sources: tuple[str, ...] = ("fts",),
    *,
    critical: bool = False,
) -> ContextItem:
    return ContextItem(candidate=candidate, score=score, sources=list(sources), critical=critical)


def _code(id_: str, source: Source = Source.EXACT, rank: int = 1, path: str = "a.py") -> Candidate:
    kind = CandidateKind.SYMBOL if id_.startswith("symbol:") else CandidateKind.FILE
    return Candidate(
        id=id_, kind=kind, title=id_, path=path, source=source, rank=rank, scope="committed"
    )


def test_entities_from_prose_backticks_and_hints() -> None:
    text = (
        "Why does `PaymentGateway.charge` retry? See shop/payments/retry.py and compute_backoff, "
        "also MAX_ATTEMPTS and the OrderService class."
    )
    ents = {
        (e.text, e.kind): e.explicit
        for e in extract_entities(text, symbols=["Hinted"], paths=["x/y.py"])
    }
    assert ents[("shop/payments/retry.py", "path")] is False
    assert ents[("compute_backoff", "identifier")] is False
    assert ents[("MAX_ATTEMPTS", "identifier")] is False
    assert ents[("OrderService", "identifier")] is False
    assert ents[("Hinted", "identifier")] is True and ents[("x/y.py", "path")] is True
    assert ents[("PaymentGateway.charge", "identifier")] is True  # backticked = explicit
    assert ("charge", "identifier") in ents  # dotted names also contribute their parts


def test_entities_ignore_ordinary_words_and_stopwords() -> None:
    assert extract_entities("What is the policy for storing card numbers?") == []
    assert [e.text for e in extract_entities("The API returns JSON")] == []


def test_or_query_does_not_require_every_word() -> None:
    q = or_query("How does the payment retry policy work for failed payments?")
    assert " or " in q and "payment" in q and "the" not in q.split(" or ")
    assert significant_terms("a an the of") == []
    assert or_query("a the") == "a the"  # nothing significant: unchanged


def test_rrf_rewards_agreement_between_sources() -> None:
    both = _k("both")
    only_fts = _k("fts-only")
    only_vec = _k("vec-only", Source.VECTOR)
    items = rrf_fuse(
        {
            "fts": [only_fts.model_copy(update={"rank": 1}), both.model_copy(update={"rank": 2})],
            "vector": [
                only_vec.model_copy(update={"rank": 1}),
                both.model_copy(update={"rank": 2, "source": Source.VECTOR}),
            ],
        }
    )
    assert items[0].candidate.id == "both"
    assert items[0].sources == ["fts", "vector"] and items[0].reasons == ["fts#2", "vector#2"]


def test_exact_matches_outweigh_other_sources_at_equal_rank() -> None:
    items = rrf_fuse(
        {
            "fts": [_k("k", Source.FTS, 1)],
            "exact": [_code("symbol:a.py::f", Source.EXACT, 1)],
        }
    )
    assert items[0].candidate.id == "symbol:a.py::f"


def test_fusion_prefers_overlay_version_and_keeps_content() -> None:
    committed = _code("symbol:a.py::f", Source.EXACT).model_copy(update={"scope": "committed"})
    overlay = _code("symbol:a.py::f", Source.SYMBOL).model_copy(
        update={"scope": "overlay", "content": "new"}
    )
    items = rrf_fuse({"exact": [committed], "symbol": [overlay]})
    assert len(items) == 1 and items[0].candidate.scope == "overlay"


LONG = " ".join(f"word{n}" for n in range(60))


def test_dedupe_marks_exact_and_near_duplicates_but_keeps_distinct_items() -> None:
    near = LONG.replace("word30", "changed")  # one word differs: still ~0.9 similar
    a = _item(_k("a", title="Retry", content=LONG, subject="retry"), 0.9)
    b = _item(_k("b", title="Retry", content=near, subject="retry"), 0.5)
    same = _item(_k("c", title="Retry", content=LONG, subject="retry"), 0.4)
    other = _item(
        _k("d", title="Naming", content="use snake case for modules everywhere always"), 0.3
    )
    result, redundant = dedupe([a, b, same, other])
    flagged = {i.candidate.id: i.redundant_of for i in result}
    assert flagged["a"] is None and flagged["d"] is None
    assert flagged["b"] == "a" and flagged["c"] == "a"
    assert set(redundant) == {"b", "c"}


def test_near_duplicates_need_the_same_subject_but_identical_text_is_always_a_duplicate() -> None:
    near = LONG.replace("word30", "changed")
    first = _item(_k("first", content=LONG, subject="x"), 1.0)
    near_other_subject = _item(_k("near", content=near, subject="y"), 0.5)
    near_same_subject = _item(
        _k("nearx", content=LONG.replace("word31", "other"), subject="x"), 0.5
    )
    identical_other_subject = _item(_k("same", content=LONG, subject="y"), 0.4)
    flagged = {
        i.candidate.id: i.redundant_of
        for i in dedupe([first, near_other_subject, near_same_subject, identical_other_subject])[0]
    }
    assert flagged["near"] is None  # similar but about a different subject: kept
    assert flagged["nearx"] == "first"
    assert flagged["same"] == "first"  # byte-for-byte equal text is redundant whatever the subject


def test_file_is_redundant_when_its_symbols_are_present_unless_named_exactly() -> None:
    sym = _item(_code("symbol:a.py::f", Source.FTS))
    graph_file = _item(_code("file:a.py", Source.GRAPH), 0.5, ("graph",))
    named_file = _item(_code("file:a.py", Source.EXACT), 0.5, ("exact",))
    assert dedupe([sym, graph_file])[0][1].redundant_of == "symbol:a.py"
    assert dedupe([sym, named_file])[0][1].redundant_of is None


def test_reranker_trust_health_and_freshness() -> None:
    now = datetime.now(UTC)

    def mk(id_: str, trust: Trust, health: Health, updated: datetime | None = None) -> ContextItem:
        return _item(_k(id_, trust=trust, health=health, title="retry policy", updated=updated))

    items = [
        mk("raw", Trust.RAW, Health.UNVERIFIED),
        mk("stale", Trust.DERIVED, Health.STALE),
        mk("bad", Trust.DERIVED, Health.CONTRADICTED),
        mk("old", Trust.VERIFIED, Health.CURRENT, now - timedelta(days=900)),
        mk("good", Trust.VERIFIED, Health.CURRENT),
    ]
    ranked = HeuristicReranker(now=now).rerank(items, "retry policy", level_rank=1)
    assert [i.candidate.id for i in ranked] == ["good", "old", "raw", "stale", "bad"]
    assert "health:STALE" in next(i for i in ranked if i.candidate.id == "stale").reasons


def test_reranker_prefers_named_entities_and_demotes_tests_below_l3() -> None:
    exact = _item(_code("symbol:a.py::f"), sources=("exact",))
    plain = _item(_code("symbol:b.py::g", Source.FTS, path="b.py"))
    test = _item(_code("symbol:tests/test_a.py::t", Source.FTS, path="tests/test_a.py"))
    low = HeuristicReranker().rerank([plain, test, exact], "", level_rank=1)
    assert low[0].candidate.id == "symbol:a.py::f"
    assert low[-1].candidate.id == "symbol:tests/test_a.py::t"
    deep = {
        i.candidate.id: i.score for i in HeuristicReranker().rerank([plain, test], "", level_rank=3)
    }
    assert deep["symbol:tests/test_a.py::t"] == deep["symbol:b.py::g"]  # not demoted at L3+


def test_budget_keeps_critical_items_and_reports_truncation() -> None:
    def item(id_: str, score: float, size: int, *, critical: bool = False) -> ContextItem:
        return ContextItem(
            candidate=_k(id_, content="x" * size), score=score, sources=["fts"], critical=critical
        )

    items = [
        item("big-critical", 0.1, 4000, critical=True),
        item("a", 0.9, 400),
        item("b", 0.8, 400),
        item("c", 0.7, 400),
    ]
    selected, dropped, truncated = select_within_budget(items, budget=1100, limit=10)
    ids = [i.candidate.id for i in selected]
    assert "big-critical" in ids and truncated  # critical is never trimmed, even over budget
    assert {i.candidate.id for i in dropped} == {"a", "b", "c"}  # nothing else fits after it

    selected, dropped, truncated = select_within_budget(items[1:], budget=100_000, limit=2)
    assert [i.candidate.id for i in selected] == ["a", "b"] and not truncated  # limit, not budget


def test_budget_skips_redundant_items() -> None:
    a = _item(_k("a"))
    dup = _item(_k("dup"), 0.9).model_copy(update={"redundant_of": "a"})
    selected, _, _ = select_within_budget([a, dup], 10_000, 10)
    assert [i.candidate.id for i in selected] == ["a"]


def test_levels_progress_and_stop() -> None:
    assert ContextLevel.L0.next() is ContextLevel.L1 and ContextLevel.L4.next() is None
    assert ContextLevel.L2.rank == 2
    assert estimate_tokens("") == 1 and estimate_tokens("x" * 40) == 10
    assert (
        cost_of(ContextItem(candidate=_k("a", title="x" * 8, content="y" * 8), score=1, sources=[]))
        == 2 + 2 + 16
    )


def test_low_volume_sources_keep_their_best_items_despite_crowding() -> None:
    crowd = [_item(_k(f"k{n}"), 1.0 - n / 1000) for n in range(40)]
    memory = _item(_k("mem"), 0.001, ("memory",))
    decision = _item(_k("dec"), 0.0005, ("decision",))
    history = [_item(_k(f"h{n}"), 0.0001, ("git_history",)) for n in range(5)]
    selected, dropped, _ = select_within_budget([*crowd, memory, decision, *history], 100_000, 10)
    ids = {i.candidate.id for i in selected}
    assert {"mem", "dec"} <= ids  # kept although ranked far below the cut
    assert len({i for i in ids if i.startswith("h")}) == 2  # at most 2 per reserved source
    assert len(selected) == 10  # reserved items use slots of the limit, they do not exceed it
    assert len([i for i in ids if i.startswith("k")]) == 6  # so the crowd gets the other six
    assert any(i.candidate.id.startswith("h") for i in dropped)  # the rest still dropped
