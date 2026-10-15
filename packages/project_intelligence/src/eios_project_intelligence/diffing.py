"""A line diff between two versions of one file, the way an editor's "changes" view shows it.

Pure: text in, plain data out. Used for committed (HEAD) vs working copy (uncommitted).
"""

from __future__ import annotations

import difflib
from typing import Any

MAX_SIDE_LINES = 20_000  # a bigger file is reported as too large, not diffed
MAX_OUTPUT_LINES = 3_000  # lines handed to the client; the counts always cover the whole diff


def _lines(text: str | None) -> list[str]:
    if text is None:
        return []
    lines = text.split("\n")
    if lines and lines[-1] == "":  # a final newline ends the last line, it does not start one
        lines.pop()
    return lines


def diff_text(old: str | None, new: str | None, *, context: int = 3) -> dict[str, Any]:
    """``old`` / ``new`` are whole file texts (``None`` = the file does not exist on that side).

    Returns ``{additions, deletions, hunks, truncated, too_large}``; every hunk has
    ``old_start`` / ``new_start`` (1-based) and ``lines`` of ``{t, o, n, s}`` where ``t`` is
    ``" "`` (same), ``"+"`` (added) or ``"-"`` (removed) and ``o`` / ``n`` are the line numbers.
    """
    a, b = _lines(old), _lines(new)
    if len(a) > MAX_SIDE_LINES or len(b) > MAX_SIDE_LINES:
        return {"additions": None, "deletions": None, "hunks": [], "truncated": False, "too_large": True}
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    additions = deletions = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            deletions += i2 - i1
        if tag in ("replace", "insert"):
            additions += j2 - j1

    hunks: list[dict[str, Any]] = []
    emitted = 0
    truncated = False
    for group in matcher.get_grouped_opcodes(context):
        lines: list[dict[str, Any]] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                lines += [{"t": " ", "o": i + 1, "n": j + 1, "s": a[i]} for i, j in zip(range(i1, i2), range(j1, j2), strict=True)]
                continue
            if tag in ("replace", "delete"):
                lines += [{"t": "-", "o": i + 1, "n": None, "s": a[i]} for i in range(i1, i2)]
            if tag in ("replace", "insert"):
                lines += [{"t": "+", "o": None, "n": j + 1, "s": b[j]} for j in range(j1, j2)]
        room = MAX_OUTPUT_LINES - emitted
        if room <= 0:
            truncated = True
            break
        if len(lines) > room:
            lines, truncated = lines[:room], True
        emitted += len(lines)
        hunks.append({"old_start": group[0][1] + 1, "new_start": group[0][3] + 1, "lines": lines})
        if truncated:
            break
    return {"additions": additions, "deletions": deletions, "hunks": hunks, "truncated": truncated, "too_large": False}
