"""Uncommitted-change diffs and the short, cached, per-version file summaries."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from eios_domain.project import FileScope
from eios_project_intelligence.diffing import MAX_SIDE_LINES, diff_text
from eios_project_intelligence.insight import (
    PROMPT_VERSION,
    Completion,
    CompletionError,
    FileInsight,
    build_facts,
    parse_summary,
)


# -- diff --------------------------------------------------------------------------------------
def test_diff_counts_and_numbers_lines() -> None:
    old = "a\nb\nc\nd\ne\n"
    new = "a\nB\nc\nd\ne\nf\n"
    d = diff_text(old, new, context=1)
    assert (d["additions"], d["deletions"]) == (2, 1)
    lines = [ln for h in d["hunks"] for ln in h["lines"]]
    assert {"t": "-", "o": 2, "n": None, "s": "b"} in lines
    assert {"t": "+", "o": None, "n": 2, "s": "B"} in lines
    assert {"t": "+", "o": None, "n": 6, "s": "f"} in lines


def test_diff_of_new_and_removed_files() -> None:
    added = diff_text(None, "x\ny\n")
    assert (added["additions"], added["deletions"]) == (2, 0)
    removed = diff_text("x\ny\n", None)
    assert (removed["additions"], removed["deletions"]) == (0, 2)


def test_diff_of_identical_text_is_empty() -> None:
    d = diff_text("same\n", "same\n")
    assert d["hunks"] == [] and (d["additions"], d["deletions"]) == (0, 0)


def test_a_huge_file_is_reported_not_diffed() -> None:
    big = "\n".join("x" for _ in range(MAX_SIDE_LINES + 1))
    assert diff_text(big, "y")["too_large"] is True


# -- parsing the model's answer ----------------------------------------------------------------
def test_parse_plain_json() -> None:
    s = parse_summary(
        '{"purpose": "Stores candles.", "connects": "Used by importers.", "useful": ""}'
    )
    assert s is not None and s.purpose == "Stores candles." and s.useful == ""


def test_parse_tolerates_fences_chatter_and_lists() -> None:
    raw = (
        'Sure!\n```json\n{"purpose": "Caches things", '
        '"connects": ["src/api", "tests"], "useful": null}\n```'
    )
    s = parse_summary(raw)
    assert s is not None
    assert s.connects == "src/api, tests"
    assert s.useful == ""


def test_parse_keeps_it_short() -> None:
    long = " ".join(f"w{i}" for i in range(60))
    s = parse_summary(f'{{"purpose": "{long}", "connects": "{long}", "useful": "{long}"}}')
    assert s is not None
    assert len(s.purpose.split()) <= 14 and s.purpose.endswith("…")
    assert len(s.connects.split()) <= 18


@pytest.mark.parametrize(
    "raw", ["", "no json here", "[1, 2]", '{"connects": "x"}', '{"purpose": ""}']
)
def test_parse_rejects_answers_without_a_purpose(raw: str) -> None:
    assert parse_summary(raw) is None


def test_facts_name_folders_and_externals_but_not_the_file_itself() -> None:
    detail: dict[str, Any] = {
        "language": "python", "lines": 80,
        "symbols": [{"kind": "class", "qualified_name": "Cache", "start_line": 1, "end_line": 60}],
        "relations": [
            {
                "direction": "out",
                "kind": "imports",
                "other": {"path": "src/util.py", "label": "util"},
            },
            {"direction": "out", "kind": "imports", "other": {"path": None, "label": "numpy"}},
            {"direction": "in", "kind": "imports", "other": {"path": "src/api/a.py", "label": "a"}},
            {"direction": "in", "kind": "calls", "other": {"path": "tests/t.py", "label": "t"}},
            {
                "direction": "in",
                "kind": "calls",
                "other": {"path": "src/cache.py", "label": "self"},
            },
        ],
    }  # fmt: skip
    facts = build_facts("src/cache.py", detail, "class Cache: ...")
    assert "class Cache" in facts and "src/util.py" in facts and "numpy" in facts
    assert "src/api: 1" in facts and "tests: 1" in facts
    assert "src/cache.py: 1" not in facts  # its own calls are not "used by"


# -- the service -------------------------------------------------------------------------------
@dataclass
class Row:
    path: str
    content_hash: str
    status: str = "active"


class FakeStore:
    def __init__(self) -> None:
        self.summaries: dict[tuple[uuid.UUID, str], dict[str, Any]] = {}
        self.rows: dict[tuple[str, FileScope], Row] = {}

    async def list_files(
        self, project_id: uuid.UUID, branch: str, *, scope: FileScope, path_prefix: str, limit: int
    ) -> list[Row]:
        row = self.rows.get((path_prefix, scope))
        return [row] if row else []

    async def get_file_summary(self, project_id: uuid.UUID, path: str) -> dict[str, Any] | None:
        return self.summaries.get((project_id, path))

    async def put_file_summary(self, project_id: uuid.UUID, path: str, **v: Any) -> None:
        self.summaries[(project_id, path)] = {**v, "created_at": datetime.now(UTC)}


@dataclass
class FakeProject:
    last_commit: str = "a" * 40
    last_branch: str = "main"


class FakeProjects:
    def __init__(self) -> None:
        self.store = FakeStore()
        self.sources: dict[tuple[str, bool], dict[str, Any]] = {}

    async def require(self, project_id: uuid.UUID) -> FakeProject:
        return FakeProject()

    async def effective_branch(self, project: Any, branch: str | None) -> str:
        return "main"

    async def file_detail(self, project_id: uuid.UUID, path: str) -> dict[str, Any]:
        return {"language": "python", "lines": 3, "symbols": [], "relations": []}

    async def read_source(
        self,
        project_id: uuid.UUID,
        path: str,
        *,
        prefer_overlay: bool = True,
        max_lines: int = 200,
        **_: Any,
    ) -> dict[str, Any] | None:
        return self.sources.get((path, prefer_overlay))


class Model:
    def __init__(self, *replies: str | Exception) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.up = True

    async def __call__(self, system: str, user: str) -> Completion:
        self.calls += 1
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return Completion(text=reply, model="m", provider="p", input_tokens=10, output_tokens=5)

    def available(self) -> bool:
        return self.up


GOOD = '{"purpose": "Stores candles.", "connects": "Used by importers.", "useful": ""}'
PID = uuid.uuid4()


def make(model: Model) -> tuple[FileInsight, FakeProjects]:
    projects = FakeProjects()
    projects.store.rows[("a.py", FileScope.COMMITTED)] = Row("a.py", "h1")
    projects.sources[("a.py", True)] = {"path": "a.py", "scope": "committed", "text": "x = 1\n"}
    return FileInsight(projects, model, model.available), projects  # type: ignore[arg-type]


async def test_a_summary_is_written_once_and_reused() -> None:
    model = Model(GOOD)
    insight, _ = make(model)
    assert (await insight.state(PID, "a.py")).status == "missing"
    first = await insight.summarise(PID, "a.py")
    assert (
        first.status == "ready"
        and first.summary is not None
        and first.summary.purpose == "Stores candles."
    )
    again = await insight.summarise(PID, "a.py")
    assert again.status == "ready" and model.calls == 1


async def test_a_changed_file_is_summarised_again() -> None:
    model = Model(GOOD, '{"purpose": "Now different.", "connects": "x"}')
    insight, projects = make(model)
    await insight.summarise(PID, "a.py")
    projects.store.rows[("a.py", FileScope.OVERLAY)] = Row("a.py", "h2")  # an uncommitted edit
    stale = await insight.state(PID, "a.py")
    assert stale.status == "stale" and stale.summary is not None  # the old words are still shown
    fresh = await insight.summarise(PID, "a.py")
    assert (
        fresh.status == "ready"
        and fresh.summary is not None
        and fresh.summary.purpose == "Now different."
    )
    assert model.calls == 2


async def test_a_new_prompt_version_rewrites_summaries() -> None:
    insight, projects = make(Model(GOOD))
    await insight.summarise(PID, "a.py")
    row = projects.store.summaries[(PID, "a.py")]
    row["content_hash"] = f"h1:{PROMPT_VERSION - 1}"  # written by an older prompt
    assert (await insight.state(PID, "a.py")).status == "stale"


async def test_without_a_model_nothing_is_called() -> None:
    model = Model(GOOD)
    model.up = False
    insight, _ = make(model)
    assert (await insight.summarise(PID, "a.py")).status == "no_model"
    assert model.calls == 0


async def test_a_failing_model_is_reported_and_keeps_the_old_summary() -> None:
    model = Model(GOOD, CompletionError("provider unreachable"))
    insight, projects = make(model)
    await insight.summarise(PID, "a.py")
    projects.store.rows[("a.py", FileScope.OVERLAY)] = Row("a.py", "h2")
    out = await insight.summarise(PID, "a.py")
    assert out.status == "error" and "unreachable" in (out.error or "")
    assert out.summary is not None and out.summary.purpose == "Stores candles."


async def test_a_nonsense_answer_is_an_error_not_a_summary() -> None:
    insight, projects = make(Model("I cannot help with that"))
    out = await insight.summarise(PID, "a.py")
    assert out.status == "error"
    assert (PID, "a.py") not in projects.store.summaries


async def test_diff_statuses() -> None:
    insight, projects = make(Model(GOOD))
    projects.sources[("a.py", False)] = {
        "path": "a.py",
        "scope": "committed",
        "text": "x = 1\r\ny = 2\r\n",
    }
    # working copy on disk is the same text with different line endings: not a change
    projects.sources[("a.py", True)] = {
        "path": "a.py",
        "scope": "overlay",
        "text": "x = 1\ny = 2\n",
    }
    same = await insight.diff(PID, "a.py")
    assert same["status"] == "unchanged" and same["line_endings_only"] is True
    projects.sources[("a.py", True)] = {
        "path": "a.py",
        "scope": "overlay",
        "text": "x = 1\ny = 3\nz = 4\n",
    }
    changed = await insight.diff(PID, "a.py")
    assert changed["status"] == "modified" and (changed["additions"], changed["deletions"]) == (
        2,
        1,
    )
    projects.sources[("a.py", False)] = None  # type: ignore[assignment]
    added = await insight.diff(PID, "a.py")
    assert added["status"] == "added" and added["additions"] == 3
