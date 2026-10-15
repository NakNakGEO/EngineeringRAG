"""What changed in a file, and a few words on what it is for.

* ``diff``: committed (HEAD) against the working copy, no model involved.
* ``summary_*``: three very short lines (purpose / connects / useful) written once by a model for
  one exact version of the file (its content hash) and kept; a changed file is summarised again.

The model is injected as a plain callable, so this package depends on no LLM code and tests run
without one. Nothing here ever calls a model unless one was configured.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from eios_core.logging import get_logger
from eios_domain.ids import utcnow
from eios_project_intelligence.diffing import diff_text
from eios_project_intelligence.service import ProjectService

_log = get_logger("eios.insight")

PROMPT_VERSION = 1  # bump it and every summary is rewritten the next time it is looked at
SOURCE_HEAD_LINES = 140
SOURCE_HEAD_CHARS = 5_500
WORD_LIMITS = {"purpose": 14, "connects": 18, "useful": 18}

SYSTEM_PROMPT = """You write one-glance notes about a source file for a developer who will read only a few words.
Reply with JSON only, exactly these keys: {"purpose": "...", "connects": "...", "useful": "..."}
- purpose: what the file is for, at most 12 words, plain language, do not repeat the file name.
- connects: where it is used or what it depends on, at most 14 words; name real folders or files from the facts.
- useful: one thing worth knowing (an entry point, a risk, a gotcha), at most 14 words; "" if nothing stands out.
Use only the facts and code given. If unsure, say less. No markdown, no extra keys."""


class CompletionError(Exception):
    """The model could not be used (unreachable, refused by policy, bad reply)."""


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0


# (system prompt, user prompt) -> the model's answer
Completer = Callable[[str, str], Awaitable[Completion]]


class Summary(BaseModel):
    purpose: str
    connects: str
    useful: str = ""


class SummaryState(BaseModel):
    status: Literal["ready", "stale", "missing", "no_model", "error"]
    summary: Summary | None = None
    model: str | None = None
    provider: str | None = None
    generated_at: datetime | None = None
    content_hash: str | None = None
    error: str | None = None


# -- parsing -----------------------------------------------------------------------------------
def _clamp(text: str, words: int) -> str:
    parts = text.strip().strip('"').split()
    out = " ".join(parts[:words])
    return out + "…" if len(parts) > words else out


def parse_summary(text: str) -> Summary | None:
    """Pull the three lines out of a model answer; tolerant of code fences and chatter."""
    body = text.strip()
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", body, flags=re.IGNORECASE)
    data: Any = None
    for candidate in (body, *re.findall(r"\{.*?\}", body, flags=re.DOTALL)):
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                break
        except ValueError:
            data = None
    if not isinstance(data, dict):
        return None
    def text_of(value: Any) -> str:
        if isinstance(value, list):
            return ", ".join(str(v) for v in value if v)
        return str(value or "")

    fields = {k: _clamp(text_of(data.get(k)), n) for k, n in WORD_LIMITS.items()}
    if not fields["purpose"]:
        return None
    return Summary(**fields)


# -- prompt ------------------------------------------------------------------------------------
def _lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _folder(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else "(root)"


def build_facts(path: str, detail: dict[str, Any], head: str) -> str:
    rels = detail.get("relations", [])
    internal_out = sorted({r["other"]["path"] for r in rels if r["direction"] == "out" and r["kind"] == "imports" and r["other"].get("path") and r["other"]["path"] != path})
    external = sorted({r["other"]["label"] for r in rels if r["direction"] == "out" and r["kind"] == "imports" and not r["other"].get("path")})
    users = {r["other"]["path"] for r in rels if r["direction"] == "in" and r["other"].get("path") and r["other"]["path"] != path}
    by_folder = Counter(_folder(p) for p in users)
    symbols = sorted(detail.get("symbols", []), key=lambda s: s["end_line"] - s["start_line"], reverse=True)[:18]
    lines = [f"File: {path} ({detail.get('language', '?')}, {detail.get('lines', '?')} lines)"]
    if symbols:
        lines.append("Defines: " + "; ".join(f"{s['kind']} {s['qualified_name']}" for s in symbols))
    if internal_out:
        lines.append("Imports from this project: " + ", ".join(internal_out[:8]) + (" …" if len(internal_out) > 8 else ""))
    if external:
        lines.append("Imports from outside the project: " + ", ".join(external[:10]) + (" …" if len(external) > 10 else ""))
    if by_folder:
        lines.append("Used by (folder: files): " + ", ".join(f"{f}: {n}" for f, n in by_folder.most_common(6)))
    else:
        lines.append("Used by: nothing in the project imports or calls it.")
    lines.append("Start of the file:\n" + head[:SOURCE_HEAD_CHARS])
    return "\n".join(lines)


# -- service -----------------------------------------------------------------------------------
class FileInsight:
    def __init__(
        self,
        projects: ProjectService,
        complete: Completer,
        available: Callable[[], bool],
        *,
        concurrency: int = 2,
    ) -> None:
        self._projects = projects
        self._complete = complete
        self._available = available
        self._gate = asyncio.Semaphore(concurrency)
        self._inflight: dict[tuple[uuid.UUID, str], asyncio.Future[SummaryState]] = {}
        self._cooldown: dict[tuple[uuid.UUID, str], float] = {}

    # -- changes ----------------------------------------------------------------------------
    async def diff(self, project_id: uuid.UUID, path: str) -> dict[str, Any]:
        """Working copy against the last commit. ``status``: modified | added | deleted | unchanged."""
        committed = await self._projects.read_source(project_id, path, prefer_overlay=False, max_lines=10**9)
        working = await self._projects.read_source(project_id, path, prefer_overlay=True, max_lines=10**9)
        project = await self._projects.require(project_id)
        base = (project.last_commit or "")[:8] or None
        # Windows checkouts (autocrlf) store LF and write CRLF: only real edits count as changes.
        old = _lf(committed["text"]) if committed else None
        raw_new = working["text"] if working else None
        line_endings_only = False
        if working is None:
            new, status = None, ("deleted" if old is not None else "missing")
        elif committed is None:
            new, status = _lf(working["text"]), "added"
        elif working["scope"] == "committed":
            new, status = _lf(working["text"]), "unchanged"
        else:
            new = _lf(working["text"])
            status = "unchanged" if new == old else "modified"
            line_endings_only = status == "unchanged" and raw_new != committed["text"]
        out: dict[str, Any] = {
            "path": path, "status": status, "base_commit": base, "line_endings_only": line_endings_only,
        }  # fmt: skip
        if status in ("modified", "added", "deleted"):
            out.update(diff_text(old, new))
        else:
            out.update({"additions": 0, "deletions": 0, "hunks": [], "truncated": False, "too_large": False})
        return out

    # -- summaries --------------------------------------------------------------------------
    async def _version(self, project_id: uuid.UUID, path: str) -> str | None:
        """The key of the file as it is now: content hash of the working copy (or HEAD)."""
        project = await self._projects.require(project_id)
        branch = await self._projects.effective_branch(project, None)
        from eios_domain.project import FileScope

        for scope in (FileScope.OVERLAY, FileScope.COMMITTED):
            rows = await self._projects.store.list_files(project_id, branch, scope=scope, path_prefix=path, limit=5)
            row = next((r for r in rows if r.path == path), None)
            if row is not None:
                return None if row.status == "deleted" else f"{row.content_hash}:{PROMPT_VERSION}"
        return None

    async def state(self, project_id: uuid.UUID, path: str) -> SummaryState:
        key = await self._version(project_id, path)
        cached = await self._projects.store.get_file_summary(project_id, path)
        if key is None:
            return SummaryState(status="missing")
        if cached is None:
            return SummaryState(status="missing" if self._available() else "no_model", content_hash=key)
        base = SummaryState(
            status="ready" if cached["content_hash"] == key else "stale",
            summary=Summary(**cached["summary"]), model=cached["model"], provider=cached["provider"],
            generated_at=cached["created_at"], content_hash=key,
        )  # fmt: skip
        if base.status == "stale" and not self._available():
            base.status = "no_model"
        return base

    async def summarise(self, project_id: uuid.UUID, path: str, *, force: bool = False) -> SummaryState:
        """Make sure the file has a summary for its current version (one model call, at most)."""
        current = await self.state(project_id, path)
        if current.status == "ready" and not force:
            return current
        if current.content_hash is None:  # not indexed, or deleted
            return current
        if not self._available():  # state() already says "no_model" and keeps any older summary
            return current
        slot = (project_id, path)
        running = self._inflight.get(slot)
        if running is not None:
            return await running
        future: asyncio.Future[SummaryState] = asyncio.get_running_loop().create_future()
        self._inflight[slot] = future
        try:
            result = await self._generate(project_id, path, current)
            future.set_result(result)
            return result
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
                future.exception()  # consumed: nobody else may be waiting
            raise
        finally:
            self._inflight.pop(slot, None)

    async def _generate(self, project_id: uuid.UUID, path: str, current: SummaryState) -> SummaryState:
        key = current.content_hash
        detail = await self._projects.file_detail(project_id, path)
        head = await self._projects.read_source(project_id, path, max_lines=SOURCE_HEAD_LINES)
        if detail is None or head is None or key is None:
            return SummaryState(status="missing")
        facts = build_facts(path, detail, head["text"])
        try:
            async with self._gate:
                done = await self._complete(SYSTEM_PROMPT, facts)
        except CompletionError as exc:
            return SummaryState(status="error", error=str(exc)[:300], content_hash=key, summary=current.summary)
        summary = parse_summary(done.text)
        if summary is None:
            return SummaryState(status="error", error="the model did not answer in the expected shape", content_hash=key, summary=current.summary)
        await self._projects.store.put_file_summary(
            project_id, path, content_hash=key, summary=summary.model_dump(), provider=done.provider,
            model=done.model, input_tokens=done.input_tokens, output_tokens=done.output_tokens,
        )  # fmt: skip
        return SummaryState(
            status="ready", summary=summary, model=done.model, provider=done.provider,
            generated_at=utcnow(), content_hash=key,
        )  # fmt: skip

    async def refresh_changed(self, project_id: uuid.UUID, paths: list[str], *, limit: int = 4, cooldown: float = 90.0) -> int:
        """After a sync: files that already have a summary and changed get a new one (best effort)."""
        if not self._available():
            return 0
        import time

        now = time.monotonic()
        done = 0
        for path in paths:
            if done >= limit:
                break
            slot = (project_id, path)
            if now - self._cooldown.get(slot, -1e9) < cooldown:
                continue
            if await self._projects.store.get_file_summary(project_id, path) is None:
                continue
            self._cooldown[slot] = now
            try:
                state = await self.summarise(project_id, path)
            except Exception as exc:  # a refresh must never break a sync
                _log.warning("summary_refresh_failed", path=path, error=repr(exc))
                continue
            if state.status == "ready":
                done += 1
        return done
