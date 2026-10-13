"""Which code is the request *about*: the seeds readiness and protection are measured against."""

from __future__ import annotations

from collections.abc import Iterable

from eios_retrieval.models import CandidateKind, ContextItem, Source

MAX_NAMED_SEEDS = 10


def named_code(items: Iterable[ContextItem]) -> list[ContextItem]:
    """Code the request names (exact matches) plus the members of a named class.

    Asking about ``RetryPolicy`` is asking about its methods, so what *they* call matters; a
    lexical neighbour that merely shares a word with the question is not the requested code, and
    holding the pack to its dependencies would demand context nobody asked for.
    """
    code = [i for i in items if i.candidate.kind in {CandidateKind.SYMBOL, CandidateKind.FILE}]
    exact = [i for i in code if Source.EXACT.value in i.sources]
    owners = tuple(f"{i.candidate.id}." for i in exact if i.candidate.kind is CandidateKind.SYMBOL)
    members = [
        i
        for i in code
        if Source.EXACT.value not in i.sources
        and i.candidate.kind is CandidateKind.SYMBOL
        and i.candidate.id.startswith(owners)
    ]
    return [*exact, *members][:MAX_NAMED_SEEDS]
