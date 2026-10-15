from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eios_domain.knowledge import Health, Trust
from eios_knowledge.text import or_query, significant_terms, tokenize
from eios_retrieval.entities import extract_entities
from eios_retrieval.fusion import HeuristicReranker, dedupe, estimate_tokens, rrf_fuse
from eios_retrieval.governor import ContextGovernor, _better, cost_of, select_within_budget
from eios_retrieval.graph import DEPENDENCIES_PER_SEED
from eios_retrieval.intent import detect_intent
from eios_retrieval.models import (
    Candidate,
    CandidateKind,
    ContextItem,
    ContextLevel,
    ContextPack,
    CoverageReport,
    InformationGap,
    MissingContext,
    Source,
)
from eios_retrieval.seeds import named_code
from eios_retrieval.terms import covered_terms, query_terms, stem, stems_match


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


def test_dotfile_paths_keep_their_leading_dot_and_bare_readme_is_a_file() -> None:
    ents = {
        (e.text, e.kind)
        for e in extract_entities(
            "Why does .github/workflows/ci.yml run and what is in .env.example? Fix the README."
        )
    }
    assert (".github/workflows/ci.yml", "path") in ents
    assert (".env.example", "path") in ents
    assert ("README.md", "path") in ents
    assert ("github/workflows/ci.yml", "path") not in ents
    assert ("Dockerfile", "path") in {
        (e.text, e.kind) for e in extract_entities("Why does the Dockerfile copy manifests?")
    }


def test_acronyms_and_shouted_words_are_not_identifiers_but_constants_are() -> None:
    names = {
        e.text
        for e in extract_entities(
            "Why can't I just DELETE old rows in YAML over MCP, and where is EVENT_TYPES defined?"
        )
    }
    assert "EVENT_TYPES" in names
    assert not names & {"DELETE", "YAML", "MCP"}
    explicit = {e.text for e in extract_entities("see `DELETE`")}
    assert "DELETE" in explicit  # backticks still make a bare word explicit


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


# -- request intent, term selection, pack choice (dogfood findings 2, 6, 7) -----------------------


def test_intent_detects_what_the_question_needs_beyond_its_entities() -> None:
    assert detect_intent("Which tests cover the approval fingerprint?").asks_tests
    assert detect_intent("What would break if I rename tests_missing?").considers_tests
    assert detect_intent("Why can't an LLM client approve its own call?").asks_rationale
    assert detect_intent("Which docs will go stale if I add a tool?").asks_docs
    assert detect_intent("What do I need to touch to add a source?").considers_tests
    assert not detect_intent("Open docs/adr/0003.md and explain").asks_docs  # a path, not a topic
    plain = detect_intent("Where is the dependency container built?")
    assert not (plain.wants_tests or plain.asks_rationale)
    # a path segment or an identifier is not a question about tests
    assert not detect_intent("Open tests/unit/test_policy.py and test_audit_log").asks_tests
    # "decide" is not "why": the gap resolver decides, it does not explain itself
    assert not detect_intent(
        "Where does the resolver decide between a skill and an agent?"
    ).wants_tests


def test_stemmer_joins_word_families_without_joining_unrelated_words() -> None:
    families = [
        ("backticked", "backticks"),
        ("routable", "routability"),
        ("resolver", "resolution"),
        ("embedder", "embeddings"),
        ("identify", "identity"),
        ("retries", "retry"),
    ]
    for a, b in families:
        assert stems_match(stem(a), stem(b)), (a, b)
    for a, b in [("code", "decode"), ("sure", "ensure"), ("event", "prevent"), ("token", "total")]:
        assert not stems_match(stem(a), stem(b)), (a, b)


def test_query_terms_drop_task_filler_and_keep_distinctive_stems_in_order() -> None:
    terms = query_terms(
        "I want to make sure the backticked names change when we update the code, and which tests"
    )
    assert "backtick" in terms
    for filler in ("want", "make", "sure", "change", "update", "code", "tests"):
        assert stem(filler) not in terms and filler not in terms
    assert terms.index("backtick") == 0  # first distinctive word stays first


def test_covered_terms_splits_what_the_pack_mentions_from_what_it_does_not() -> None:
    covered, uncovered = covered_terms(
        query_terms("flagged contradicting statements backticks"),
        tokenize("def flag_contradiction(a, b): # backticked statements"),
    )
    assert set(covered) == {"flagg", "contradict", "state", "backtick"} and uncovered == []
    covered, uncovered = covered_terms(
        query_terms("scheduled backends"), tokenize("def flag(): ...")
    )
    assert covered == [] and uncovered == ["schedul", "backend"]


def test_a_name_the_request_introduces_is_marked_new() -> None:
    ents = {
        e.text: e
        for e in extract_entities(
            "Add a nullable correlation_id column to audit_log, called `new_flag` in `RunStore`"
        )
    }
    assert ents["correlation_id"].new and ents["new_flag"].new
    assert not ents["audit_log"].new and not ents["RunStore"].new
    existing = {e.text: e for e in extract_entities("Add a retry to RetentionService.run")}
    assert not existing["RetentionService"].new  # no 'column/field/...' noun: it already exists


def _pack(
    *, ready: bool, coverage: float, blocking: int = 0, level: ContextLevel, truncated: bool = False
) -> ContextPack:
    return ContextPack(
        query="q",
        level=level,
        items=[],
        gap=InformationGap(
            ready_to_act=ready,
            missing_context=[MissingContext(kind="x", description="d", blocking=True)] * blocking,
            confidence=0.5,
            coverage=coverage,
        ),
        coverage=CoverageReport(
            entity_coverage=1,
            dependency_coverage=1,
            evidence_support=1,
            index_health=1,
            entities_total=0,
            entities_found=0,
            dependencies_total=0,
            dependencies_included=0,
        ),
        budget_tokens=100,
        used_tokens=10,
        truncated=truncated,
    )


def test_best_pack_prefers_ready_then_coverage_then_fewer_blockers_then_the_higher_level() -> None:
    l1 = _pack(ready=False, coverage=0.6, blocking=2, level=ContextLevel.L1)
    assert _better(_pack(ready=False, coverage=0.6, blocking=2, level=ContextLevel.L3), l1)  # tie
    assert _better(_pack(ready=False, coverage=0.6, blocking=1, level=ContextLevel.L2), l1)
    assert not _better(_pack(ready=False, coverage=0.5, blocking=0, level=ContextLevel.L2), l1)
    assert _better(_pack(ready=True, coverage=0.4, level=ContextLevel.L2), l1)  # ready beats all
    cut = _pack(ready=False, coverage=0.6, blocking=2, level=ContextLevel.L4, truncated=True)
    assert not _better(cut, l1)  # a tie is not worth a budget-truncated pack


def test_only_the_best_dependencies_of_a_named_seed_stay_protected() -> None:
    seed = _item(_code("symbol:a.py::Seed", Source.EXACT), 1.0, ("exact",))

    def dep(n: int, confidence: float) -> ContextItem:
        cand = _code(f"symbol:d{n}.py::d{n}", Source.GRAPH, path=f"d{n}.py").model_copy(
            update={
                "distance": 1,
                "via": "calls/out",
                "raw_score": confidence,
                "metadata": {"parent": "symbol:a.py::Seed"},
            }
        )
        return _item(cand, 0.5, ("graph",))

    deps = [dep(n, 1.0 - n / 100) for n in range(DEPENDENCIES_PER_SEED + 6)]
    marked = ContextGovernor._mark_critical([seed, *deps], [])
    critical = {i.candidate.id for i in marked if i.critical}
    assert "symbol:a.py::Seed" in critical
    assert len(critical) == 1 + DEPENDENCIES_PER_SEED  # not every neighbour is protected
    assert {f"symbol:d{n}.py::d{n}" for n in range(DEPENDENCIES_PER_SEED)} <= critical

    # a word shared with the question lifts an otherwise equal dependency into the protected set
    equal = [dep(n, 0.9) for n in range(DEPENDENCIES_PER_SEED + 2)]
    equal[-1] = equal[-1].model_copy(
        update={"candidate": equal[-1].candidate.model_copy(update={"title": "ledger helper"})}
    )
    marked = ContextGovernor._mark_critical([seed, *equal], query_terms("ledger"))
    assert equal[-1].candidate.id in {i.candidate.id for i in marked if i.critical}


def test_named_code_includes_members_of_a_named_class_but_not_lexical_neighbours() -> None:
    cls = _item(_code("symbol:a.py::Policy", Source.EXACT), 1.0, ("exact",))
    member = _item(_code("symbol:a.py::Policy.check", Source.SYMBOL), 0.5, ("symbol",))
    other = _item(_code("symbol:b.py::policy_helper", Source.SYMBOL, path="b.py"), 0.5, ("symbol",))
    seeds = [i.candidate.id for i in named_code([cls, member, other])]
    assert seeds == ["symbol:a.py::Policy", "symbol:a.py::Policy.check"]


# -- content retrieval helpers (ADR 0018) ---------------------------------------------------------
def test_content_words_drop_filler_and_short_words() -> None:
    from eios_retrieval.content import content_words

    words = content_words("I want to change the retention job so it can purge old events at 3am")
    assert "retention" in words and "purge" in words and "events" in words
    assert (
        "change" not in words and "want" not in words and "the" not in words and "am" not in words
    )


def test_document_numbers_come_from_adr_and_decision_phrases() -> None:
    from eios_retrieval.content import document_numbers

    assert document_numbers("What does ADR 0005 decide?") == [5]
    assert document_numbers("see adr-12, and Decision #7") == [12, 7]
    assert document_numbers("RFC3 and adrs 8") == [3, 8]
    assert document_numbers("the 12 decisions we made in 2024") == []
    assert document_numbers("address 0005") == []


def test_named_identifiers_keep_names_not_prose_words() -> None:
    from eios_retrieval.content import named_identifiers
    from eios_retrieval.entities import extract_entities

    entities = extract_entities(
        "Why does `eios.retention` bypass uq_event_run_seq in RetentionService for tokens?",
        symbols=["job"],
    )
    names = named_identifiers(entities)
    assert "eios.retention" in names and "retention" in names  # a dotted name and its last part
    assert "uq_event_run_seq" in names and "RetentionService" in names
    assert "job" in names  # the caller meant it
    assert "tokens" not in names and "Why" not in names


def test_a_name_to_look_up_in_text_is_never_an_ordinary_word_or_an_introduced_name() -> None:
    from eios_retrieval.entities import Entity
    from eios_retrieval.retrievers import _lookup_names

    assert _lookup_names(Entity("uq_event_run_seq", "identifier")) == ["uq_event_run_seq"]
    assert _lookup_names(Entity("eios.retention", "identifier", explicit=True)) == [
        "eios.retention",
        "retention",
    ]
    assert _lookup_names(Entity("policy.forbid_audit_mutation", "identifier")) == [
        "policy.forbid_audit_mutation",
        "forbid_audit_mutation",
    ]
    assert _lookup_names(Entity("retention", "identifier")) == []  # an ordinary word in prose
    assert _lookup_names(Entity("migrate", "identifier", explicit=True)) == ["migrate"]
    assert _lookup_names(Entity("uq_x_y", "identifier", new=True)) == []  # the request makes it
    assert _lookup_names(Entity("docs/a.md", "path")) == []


def test_files_that_mention_a_name_are_ranked_migrations_then_docs_then_config() -> None:
    from eios_project_intelligence.store import ChunkHit
    from eios_retrieval.retrievers import _content_rank

    def hit(path: str, language: str, matches: int = 1) -> ChunkHit:
        return ChunkHit(
            path=path, scope="committed", language=language, line_count=10, start_line=1,
            end_line=5, heading="", text="", matches=matches,
        )  # fmt: skip

    hits = [
        hit("config/app.yaml", "yaml", 9),
        hit("tests/test_x.py", "python", 9),
        hit("docs/adr/0002-x.md", "markdown", 1),
        hit("migrations/versions/0002_x.py", "python", 1),
        hit("docs/adr/0003-y.md", "markdown", 4),
    ]
    assert [h.path for h in sorted(hits, key=_content_rank)] == [
        "migrations/versions/0002_x.py",
        "docs/adr/0003-y.md",
        "docs/adr/0002-x.md",
        "config/app.yaml",
        "tests/test_x.py",
    ]


def test_content_is_a_weighted_source_that_the_default_levels_use() -> None:
    from eios_retrieval import LEVELS, ContextLevel
    from eios_retrieval.fusion import SOURCE_WEIGHTS

    assert SOURCE_WEIGHTS[Source.CONTENT.value] == 1.0
    assert "content" not in LEVELS[ContextLevel.L0].retrievers
    for level in (ContextLevel.L1, ContextLevel.L2, ContextLevel.L3, ContextLevel.L4):
        assert "content" in LEVELS[level].retrievers
