"""Small deterministic analyzers for text an assistant hands over. Standard library only; they read
text and return facts: no network, no files, no programs. Formats that need a parser library
(PDF, DOCX) are refused with a clear message instead of being guessed."""

from __future__ import annotations

import csv
import io
import json
import re
import statistics
from collections import Counter
from typing import Any

import yaml

from eios_runtime.web_research import html_to_text

MAX_TEXT = 2_000_000
_NOT_SUPPORTED = {"pdf", "docx", "xlsx", "pptx", "doc", "xls"}


class AnalysisError(ValueError):
    """The input cannot be analysed; the message says why."""


def _text(value: Any, name: str) -> str:
    text = str(value or "")
    if not text.strip():
        raise AnalysisError(f"'{name}' is empty")
    if len(text) > MAX_TEXT:
        raise AnalysisError(f"'{name}' is larger than {MAX_TEXT:,} characters")
    return text


# -- document_parse ----------------------------------------------------------------------------
def document_parse(args: dict[str, Any]) -> dict[str, Any]:
    """Text and structure of HTML, Markdown, JSON, YAML, CSV or plain text."""
    kind = str(args.get("format", "")).lower().lstrip(".") or _sniff(str(args.get("document", ""))[:2000])
    if kind in _NOT_SUPPORTED:
        raise AnalysisError(f"'{kind}' needs a parser library that is not installed; send its text instead")
    text = _text(args.get("document"), "document")
    if kind in {"html", "htm"}:
        title, body = html_to_text(text)
        return {"format": "html", "title": title, "text": body, "headings": re.findall(r"<h[1-3][^>]*>(.*?)</h[1-3]>", text, flags=re.I | re.S)[:50]}
    if kind in {"md", "markdown"}:
        heads = [(len(m.group(1)), m.group(2).strip()) for m in re.finditer(r"^(#{1,6})\s+(.+)$", text, flags=re.M)]
        return {"format": "markdown", "text": text, "headings": [{"level": lv, "title": t} for lv, t in heads[:100]], "links": re.findall(r"\]\((https?://[^)\s]+)\)", text)[:100]}
    if kind == "json":
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise AnalysisError(f"not valid JSON: {exc}") from exc
        return {"format": "json", "type": type(data).__name__, "top_level_keys": sorted(data)[:100] if isinstance(data, dict) else None, "length": len(data) if hasattr(data, "__len__") else None}
    if kind in {"yaml", "yml"}:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise AnalysisError(f"not valid YAML: {exc}") from exc
        return {"format": "yaml", "type": type(data).__name__, "top_level_keys": sorted(map(str, data))[:100] if isinstance(data, dict) else None}
    if kind == "csv":
        rows = list(csv.reader(io.StringIO(text)))
        return {"format": "csv", "columns": rows[0] if rows else [], "rows": max(0, len(rows) - 1), "sample": rows[1:6]}
    return {"format": "text", "text": text, "lines": text.count("\n") + 1, "words": len(text.split())}


def _sniff(head: str) -> str:
    h = head.lstrip()
    if h.startswith("<") and re.search(r"<(html|body|div|p|h1)\b", h, re.I):
        return "html"
    if h[:1] in "{[":
        return "json"
    if re.match(r"^#{1,6}\s", h):
        return "markdown"
    return "text"


# -- log_analyze -------------------------------------------------------------------------------
_LEVEL = re.compile(r"\b(TRACE|DEBUG|INFO|NOTICE|WARN(?:ING)?|ERROR|CRITICAL|FATAL|SEVERE)\b", re.I)
_STAMP = re.compile(r"\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})")
_NOISE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|\b0x[0-9a-f]+\b|\b\d+(?:\.\d+)?\b", re.I)


def log_analyze(args: dict[str, Any]) -> dict[str, Any]:
    """Levels, time span, the most repeated messages (numbers and ids folded) and error blocks."""
    text = _text(args.get("logs"), "logs")
    lines = text.splitlines()
    levels: Counter[str] = Counter()
    messages: Counter[str] = Counter()
    stamps: list[str] = []
    errors: list[dict[str, Any]] = []
    for i, line in enumerate(lines, 1):
        m = _LEVEL.search(line)
        level = m.group(1).upper().replace("WARNING", "WARN") if m else None
        if level:
            levels[level] += 1
        s = _STAMP.search(line)
        if s:
            stamps.append(s.group(1))
        body = _NOISE.sub("#", line[(m.end() if m else 0):]).strip(" :-[]")[:160]
        if body:
            messages[body] += 1
        if level in {"ERROR", "CRITICAL", "FATAL", "SEVERE"} and len(errors) < 20:
            errors.append({"line": i, "text": line.strip()[:200]})
    traces = sum(1 for ln in lines if ln.startswith("Traceback (most recent call last)") or re.match(r"^\s+at \S+\(.*\)$", ln))
    return {
        "lines": len(lines), "levels": dict(levels.most_common()),
        "first_timestamp": min(stamps) if stamps else None, "last_timestamp": max(stamps) if stamps else None,
        "top_messages": [{"message": m, "count": n} for m, n in messages.most_common(10) if n > 1],
        "errors": errors, "stack_trace_blocks": traces,
    }


# -- api_contract_analyze ----------------------------------------------------------------------
_METHODS = ("get", "put", "post", "delete", "patch", "head", "options")


def _load_spec(raw: Any, name: str) -> dict[str, Any]:
    text = _text(raw, name)
    try:
        data = json.loads(text) if text.lstrip().startswith("{") else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError) as exc:
        raise AnalysisError(f"'{name}' is not valid JSON or YAML: {exc}") from exc
    if not isinstance(data, dict) or not ("openapi" in data or "swagger" in data or "paths" in data):
        raise AnalysisError(f"'{name}' does not look like an OpenAPI / Swagger document")
    return data


def _operations(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    ops: dict[str, dict[str, Any]] = {}
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method in _METHODS:
            op = item.get(method)
            if isinstance(op, dict):
                params = {p.get("name"): bool(p.get("required")) for p in (item.get("parameters") or []) + (op.get("parameters") or []) if isinstance(p, dict)}
                ops[f"{method.upper()} {path}"] = {"params": params, "responses": sorted(map(str, (op.get("responses") or {}).keys())), "deprecated": bool(op.get("deprecated"))}
    return ops


def api_contract_analyze(args: dict[str, Any]) -> dict[str, Any]:
    """Operations, auth and schemas of one OpenAPI document; with ``previous``, what changed and
    which changes break callers (removed operations, new required parameters, removed responses)."""
    spec = _load_spec(args.get("contract"), "contract")
    ops = _operations(spec)
    comps = spec.get("components") or {}
    out: dict[str, Any] = {
        "title": (spec.get("info") or {}).get("title"), "version": (spec.get("info") or {}).get("version"),
        "operations": len(ops), "paths": len(spec.get("paths") or {}),
        "schemas": len((comps.get("schemas") or spec.get("definitions") or {})),
        "auth_schemes": sorted((comps.get("securitySchemes") or spec.get("securityDefinitions") or {}).keys()),
        "deprecated": sorted(k for k, v in ops.items() if v["deprecated"]),
        "sample": sorted(ops)[:30],
    }
    if args.get("previous"):
        before = _operations(_load_spec(args.get("previous"), "previous"))
        breaking: list[str] = []
        for key, old in before.items():
            new = ops.get(key)
            if new is None:
                breaking.append(f"removed operation {key}")
                continue
            for p, required in new["params"].items():
                if required and not old["params"].get(p, False):
                    breaking.append(f"{key}: parameter '{p}' is now required")
            for r in set(old["responses"]) - set(new["responses"]):
                breaking.append(f"{key}: response {r} was removed")
        out["compared_with_previous"] = {
            "added": sorted(set(ops) - set(before))[:50], "removed": sorted(set(before) - set(ops))[:50],
            "breaking": breaking[:100], "compatible": not breaking,
        }
    return out


# -- performance_profile -----------------------------------------------------------------------
_TIMED = re.compile(r"^\s*(?P<name>.+?)\s*[:=,]\s*(?P<v>\d+(?:\.\d+)?)\s*(?P<u>us|µs|ms|s|sec|seconds)?\s*$", re.I)
_UNIT = {"us": 0.001, "µs": 0.001, "ms": 1.0, "s": 1000.0, "sec": 1000.0, "seconds": 1000.0}


def performance_profile(args: dict[str, Any]) -> dict[str, Any]:
    """Timing statistics from ``samples`` ([{name, ms}]) or text lines like ``query users: 12.5ms``."""
    rows: list[tuple[str, float]] = []
    for s in args.get("samples") or []:
        try:
            rows.append((str(s["name"]), float(s["ms"])))
        except (KeyError, TypeError, ValueError) as exc:
            raise AnalysisError("each sample needs a 'name' and a numeric 'ms'") from exc
    if not rows and args.get("trace"):
        for line in _text(args.get("trace"), "trace").splitlines():
            m = _TIMED.match(line)
            if m:
                rows.append((m.group("name"), float(m.group("v")) * _UNIT.get((m.group("u") or "ms").lower(), 1.0)))
    if not rows:
        raise AnalysisError("no timings found: send 'samples' or a 'trace' with lines like 'name: 12.5ms'")
    by_name: dict[str, list[float]] = {}
    for name, ms in rows:
        by_name.setdefault(name, []).append(ms)
    allv = [v for _, v in rows]

    def pct(values: list[float], q: float) -> float:
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, int(q * len(ordered)))]

    table = sorted(
        ({"name": n, "count": len(v), "total_ms": round(sum(v), 3), "mean_ms": round(statistics.fmean(v), 3), "p95_ms": round(pct(v, 0.95), 3), "max_ms": round(max(v), 3)} for n, v in by_name.items()),
        key=lambda r: -r["total_ms"],
    )
    return {
        "samples": len(rows), "total_ms": round(sum(allv), 3), "p50_ms": round(statistics.median(allv), 3),
        "p95_ms": round(pct(allv, 0.95), 3), "max_ms": round(max(allv), 3), "slowest_by_total": table[:15],
        "share_of_time_top1": round(table[0]["total_ms"] / sum(allv), 3) if sum(allv) else 0.0,
    }
