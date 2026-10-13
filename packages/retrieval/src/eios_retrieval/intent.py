"""What a request is *asking for*, beyond the entities it names.

A pack is only sufficient if it holds the kind of evidence the question needs: "which tests
cover X" is not answered by X's source, and "why" is not answered without a rationale source.
The detector is a cheap, deterministic regex pass - it only ever adds requirements, so a wrong
guess costs an extra expansion and a visible gap, never a silently thinner pack.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# "tests" as a word, not as a path segment (tests/unit/...) or part of an identifier (test_x).
_ASKS_TESTS = re.compile(r"\b(?:tests?|specs?|test suites?|unit tests?)\b(?![/_\w-])", re.I)
_CONSIDERS_TESTS = re.compile(
    r"\b(?:break(?:s|ing)?|broken|renam\w+|regress\w*|affect(?:ed|s)?|impact(?:ed|s)?|"
    r"what else|ripple|blast radius|depend(?:s|ent|ents)? on|"
    r"what do i (?:need|have) to (?:touch|change|add|update)|which files)\b",
    re.I,
)
_ASKS_DOCS = re.compile(
    r"\b(?:docs?|documentation|adrs?|readme|runbook|changelog|documented|threat model)\b"
    r"(?![/_\w-])",
    re.I,
)
_ASKS_RATIONALE = re.compile(
    r"\bwhy\b|\brationale\b|\bjustif(?:y|ication|ied)\b|\btrade-?offs?\b|\bmotivation\b"
    r"|\bthe reasons?\b|\bwhat(?:'s| is) the reason\b",
    re.I,
)


@dataclass(frozen=True)
class QueryIntent:
    asks_tests: bool = False  # the question is about tests: no test evidence = a blocking gap
    considers_tests: bool = False  # a change/impact question: tests are searched for, not required
    asks_rationale: bool = False  # "why": needs a document, decision, memory or history item
    asks_docs: bool = False  # the question names documentation: a document must be in the pack

    @property
    def wants_tests(self) -> bool:
        return self.asks_tests or self.considers_tests


def detect_intent(text: str) -> QueryIntent:
    return QueryIntent(
        asks_tests=bool(_ASKS_TESTS.search(text)),
        considers_tests=bool(_CONSIDERS_TESTS.search(text)),
        asks_rationale=bool(_ASKS_RATIONALE.search(text)),
        asks_docs=bool(_ASKS_DOCS.search(text)),
    )


_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")


def is_doc_path(path: str | None) -> bool:
    if not path:
        return False
    lowered = path.lower()
    return lowered.endswith(_DOC_SUFFIXES) or lowered.startswith("docs/") or "/docs/" in lowered
