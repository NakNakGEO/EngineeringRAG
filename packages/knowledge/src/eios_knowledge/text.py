"""Shared text utilities: identifier-aware tokenisation used by embedding and retrieval."""

from __future__ import annotations

import re

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_WORD = re.compile(r"[A-Za-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lower-cased tokens; camelCase and snake_case identifiers are split into their parts.

    ``GetUserById`` -> ``get user by id``; the unsplit identifier is kept as well so exact
    identifier mentions still match.
    """
    tokens: list[str] = []
    for word in _WORD.findall(text.replace("_", " ")):
        parts = [p for p in _CAMEL.split(word) if p]
        if len(parts) > 1:
            tokens.append(word.lower())
        tokens.extend(p.lower() for p in parts)
    return tokens


_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "then",
        "else",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "for",
        "from",
        "with",
        "without",
        "into",
        "over",
        "under",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "do",
        "does",
        "did",
        "doing",
        "have",
        "has",
        "had",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "as",
        "not",
        "no",
        "yes",
        "how",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "why",
        "can",
        "could",
        "should",
        "would",
        "will",
        "shall",
        "may",
        "might",
        "must",
        "about",
        "after",
        "before",
        "between",
        "through",
        "during",
        "above",
        "below",
        "up",
        "down",
        "out",
        "off",
        "again",
        "further",
        "once",
        "here",
        "there",
        "all",
        "any",
        "both",
        "each",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "only",
        "own",
        "same",
        "so",
        "than",
        "too",
        "very",
        "just",
        "i",
        "you",
        "he",
        "she",
        "we",
        "they",
        "me",
        "him",
        "her",
        "us",
        "them",
        "my",
        "your",
        "our",
        "their",
    ]
)


def significant_terms(text: str, *, limit: int = 12) -> list[str]:
    """Distinctive lower-case terms of a question (stop words and 1-2 letter tokens removed)."""
    seen: dict[str, None] = {}
    for token in tokenize(text):
        if len(token) > 2 and token not in _STOPWORDS and not token.isdigit():
            seen.setdefault(token, None)
    return list(seen)[:limit]


def or_query(text: str, *, limit: int = 12) -> str:
    """A websearch-style OR query: documents matching *more* terms rank higher, but a document
    need not contain every word of a natural-language question."""
    terms = significant_terms(text, limit=limit)
    return " or ".join(terms) if terms else text
