"""Query-term selection shared by the symbol retriever and the coverage evaluator.

The knowledge package's stop list serves full-text search, where a common word is merely
down-weighted. Symbol and coverage matching have no such weighting, so they need the task
filler removed (``change``, ``want``, ``sure`` ...) and a light stemmer so ``backticked`` meets
``backticks`` and ``routable`` meets ``routability``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from eios_knowledge.text import significant_terms, tokenize

_FILLER_WORDS = """
    add adds added adding new update updates updated want wants need needs needed make makes made
    sure code codes default defaults work works working use uses used using thing things way ways
    change changes changed changing touch touches look looks find finds show shows get gets keep
    keeps let lets like also still even really actually already something anything everything
    instead rather without whether happen happens happening behave behaves behaviour behavior
    break breaks broken else file files function functions class classes method methods line
    lines name names type types value values case cases part parts kind kinds step steps point
    plain simple basic just long time times one two three four five single every
    test tests tested testing spec specs cover covers covered covering coverage
"""
FILLER = frozenset(_FILLER_WORDS.split())

_SUFFIXES = (
    "ility", "ation", "ement", "ments", "ings", "ions", "able", "ible", "ment", "ness", "ing",
    "ion", "ers", "ies", "ied", "ify", "ity", "ed", "er", "es", "ly", "s",
)  # fmt: skip
MIN_STEM = 4


def stem(word: str) -> str:
    """Strip one common English suffix, never leaving fewer than :data:`MIN_STEM` letters."""
    w = word.lower()
    for suffix in _SUFFIXES:
        if w.endswith(suffix) and len(w) - len(suffix) >= MIN_STEM:
            return w[: -len(suffix)]
    return w


def stems_match(a: str, b: str) -> bool:
    """Same word family: equal stems, or a long shared prefix covering nearly all of the shorter."""
    if a == b:
        return True
    shared = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        shared += 1
    return shared >= MIN_STEM and shared >= min(len(a), len(b)) - 1


def query_terms(text: str, *, limit: int = 16) -> list[str]:
    """Distinctive stems of a question, in order, without task filler or duplicates."""
    out: dict[str, None] = {}
    for term in significant_terms(text, limit=limit * 3):
        if term in FILLER or stem(term) in FILLER or len(term) < MIN_STEM:
            continue
        out.setdefault(stem(term), None)
    return list(out)[:limit]


def covered_terms(terms: Iterable[str], tokens: Iterable[str]) -> tuple[list[str], list[str]]:
    """Split ``terms`` (stems) into (covered, uncovered) by the stems of ``tokens``."""
    buckets: dict[str, set[str]] = {}
    for token in tokens:
        s = stem(token)
        buckets.setdefault(s[:4], set()).add(s)
    covered: list[str] = []
    uncovered: list[str] = []
    for term in terms:
        pool = buckets.get(term[:4], ())
        hit = term in pool or any(stems_match(term, p) for p in pool)
        (covered if hit else uncovered).append(term)
    return covered, uncovered


def term_overlap(terms: Sequence[str], text: str) -> float:
    """Share of the question's ``terms`` that ``text`` (a name, path or snippet) mentions."""
    if not terms:
        return 0.0
    covered, _ = covered_terms(terms, tokenize(text))
    return len(covered) / len(terms)
