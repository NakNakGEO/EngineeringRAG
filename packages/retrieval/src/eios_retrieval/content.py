"""Content retrieval: the text of documents, configuration, SQL and migrations.

The symbol index cannot answer "what does the threat model say about the sandbox" or "which
migration makes the event log append-only": those files have no symbols. The content index
(``source.project_chunk``) holds their text in bounded chunks, so this retriever can:

* rank chunks by the rarity of the question's words they contain (Postgres full-text search, with
  a bonus for the exact identifiers the question names), and report **one candidate per file**
  whose line range is the best matching chunk;
* find a document by name - "ADR 0005", "decision 12", "the threat model", "the runbook" - even
  when no word of its body matches.

Candidates are ``file:<path>`` ids, so fusion merges them with the same files found by the other
retrievers; two sources agreeing on a file is what makes a prose-only pack count as corroborated.
"""

from __future__ import annotations

import re
from collections import defaultdict

from eios_knowledge.text import significant_terms, tokenize
from eios_project_intelligence import ProjectService
from eios_project_intelligence.chunking import is_identifier_like
from eios_project_intelligence.store import ChunkHit
from eios_retrieval.entities import Entity
from eios_retrieval.models import Candidate, Source
from eios_retrieval.retrievers import RetrievalContext, content_candidate
from eios_retrieval.terms import FILLER, covered_terms, query_terms, stem

# "ADR 0005", "adr-12", "decision #7", "RFC 3"
_DOC_NUMBER = re.compile(r"\b(?:adr|rfc|decision)s?[\s#:_-]*(\d{1,4})\b", re.I)


def content_words(text: str, *, limit: int = 16) -> list[str]:
    """The question's words as the full-text index sees them: no task filler, 3+ letters."""
    out: dict[str, None] = {}
    for word in significant_terms(text, limit=limit * 3):
        if len(word) >= 3 and word not in FILLER and stem(word) not in FILLER:
            out.setdefault(word, None)
    return list(out)[:limit]


def document_numbers(text: str) -> list[int]:
    """ADR / decision numbers the question mentions (``ADR 0005`` -> 5)."""
    seen: dict[int, None] = {}
    for match in _DOC_NUMBER.finditer(text):
        seen.setdefault(int(match.group(1)), None)
    return list(seen)[:4]


def named_identifiers(entities: list[Entity], *, limit: int = 12) -> list[str]:
    """Exact names to look for in text: the ones the caller clearly means plus identifier-shaped
    names from the prose (a dotted name also by its last part)."""
    out: dict[str, None] = {}
    for entity in entities:
        if entity.kind != "identifier" or len(entity.text) < 3:
            continue
        if not (entity.explicit or is_identifier_like(entity.text)):
            continue
        out.setdefault(entity.text, None)
        last = entity.text.rsplit(".", 1)[-1]
        if last != entity.text and len(last) >= 3 and (entity.explicit or is_identifier_like(last)):
            out.setdefault(last, None)
    return list(out)[:limit]


class ContentRetriever:
    """Files whose text, headings or name match the question (docs, config, SQL, migrations)."""

    name = "content"
    source = Source.CONTENT
    MAX_PATH_HITS = 3  # a single question word names a file only if this few paths share it

    def __init__(self, projects: ProjectService) -> None:
        self._projects = projects

    async def retrieve(self, rc: RetrievalContext, limit: int) -> list[Candidate]:
        if rc.project is None or rc.branch is None:
            return []
        store = self._projects.store
        project_id, branch = rc.project.id, rc.branch
        text = rc.query.text
        by_text = await store.search_content(
            project_id,
            branch,
            content_words(text),
            named_identifiers(rc.entities),
            scopes=rc.scopes,
            limit=limit * 2,
        )
        stems = query_terms(text, limit=24)
        numbers = document_numbers(text)
        by_name = await store.search_content_paths(
            project_id, branch, terms=stems, numbers=numbers, scopes=rc.scopes
        )

        top = max((h.score for h in by_text), default=0.0) or 1.0
        score: dict[str, float] = {h.path: h.score / top for h in by_text}
        hits: dict[str, ChunkHit] = {h.path: h for h in by_text}
        for hit, bonus in self._named(by_name, stems, numbers):
            score[hit.path] = score.get(hit.path, 0.0) + bonus
            hits.setdefault(hit.path, hit)  # a file found only by its name shows its first chunk
        ordered = sorted(score, key=lambda p: (-score[p], p))[:limit]
        return [
            content_candidate(hits[path], Source.CONTENT, n, score[path])
            for n, path in enumerate(ordered, 1)
        ]

    def _named(
        self, found: list[ChunkHit], stems: list[str], numbers: list[int]
    ) -> list[tuple[ChunkHit, float]]:
        """Files picked by name: the numbered ADR, or two question words (or one few paths share)
        in the path."""
        numbered = set(numbers)
        covered: dict[str, list[str]] = {}
        frequency: dict[str, int] = defaultdict(int)
        for hit in found:
            words = tokenize(hit.path.replace("/", " ").replace(".", " ").replace("-", " "))
            covered[hit.path] = covered_terms(stems, words)[0]
            for term in covered[hit.path]:
                frequency[term] += 1
        out: list[tuple[ChunkHit, float]] = []
        for hit in found:
            terms = covered[hit.path]
            bonus = 0.0
            if numbered and _is_numbered(hit.path, numbered):
                bonus += 5.0
            if len(terms) >= 2:
                bonus += 1.0
            elif terms and frequency[terms[0]] <= self.MAX_PATH_HITS:
                bonus += 0.4
            if bonus:
                out.append((hit, bonus))
        return out


def _is_numbered(path: str, numbers: set[int]) -> bool:
    base = path.rsplit("/", 1)[-1]
    match = re.match(r"(?:adr[-_ ]?)?0*(\d+)[-_. ]", base, re.I)
    return match is not None and int(match.group(1)) in numbers


__all__ = [
    "ContentRetriever",
    "content_words",
    "document_numbers",
    "named_identifiers",
]
