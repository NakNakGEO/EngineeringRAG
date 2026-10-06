"""The real, in-process implementations behind the builtin tool manifests.

Every adapter takes validated arguments and returns JSON-serialisable output. None of them can
reach an external database or run a program: ``sql_analyze`` parses SQL *text* only, and
``git_inspect`` uses the hardened read-only git layer.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from eios_capability import AdapterCatalog
from eios_domain.errors import DomainError
from eios_domain.knowledge import SourceKind
from eios_project_intelligence.parsers import ParserRegistry
from eios_retrieval import RetrievalQuery

if TYPE_CHECKING:
    from eios_runtime.container import Container

MAX_SQL_BYTES = 500_000


def _uuid(args: dict[str, Any], key: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(args[key]))
    except (KeyError, ValueError) as exc:
        raise DomainError(f"'{key}' must be a UUID") from exc


def _query(args: dict[str, Any]) -> RetrievalQuery:
    fields = {k: v for k, v in args.items() if k in RetrievalQuery.model_fields}
    return RetrievalQuery.model_validate(fields)


def register_builtin_adapters(catalog: AdapterCatalog, container: Container) -> None:
    parsers = ParserRegistry()

    async def knowledge_search(args: dict[str, Any]) -> dict[str, Any]:
        result = await container.governor.search(_query(args), limit=int(args.get("limit", 20)))
        return {
            "items": [
                i.model_dump(mode="json") for i in result.items[: int(args.get("limit", 20))]
            ],
            "errors": result.errors,
        }

    async def context_build(args: dict[str, Any]) -> dict[str, Any]:
        pack = await container.governor.build(
            _query(args), token_budget=int(args.get("token_budget", 12_000))
        )
        return {"pack": pack.model_dump(mode="json")}

    async def project_sync(args: dict[str, Any]) -> dict[str, Any]:
        result, run_id = await container.projects.sync_now(_uuid(args, "project_id"))
        return {"run_id": str(run_id), "result": asdict(result)}

    async def impact_analyze(args: dict[str, Any]) -> dict[str, Any]:
        report = await container.impact.analyze(
            _uuid(args, "project_id"),
            paths=args.get("paths"),
            symbols=args.get("symbols"),
            branch=args.get("branch"),
            depth=int(args.get("depth", 3)),
        )
        return report.model_dump(mode="json")

    async def git_inspect(args: dict[str, Any]) -> dict[str, Any]:
        commits = await container.projects.history(
            _uuid(args, "project_id"),
            paths=args.get("paths"),
            grep=args.get("grep"),
            limit=int(args.get("limit", 20)),
        )
        return {"commits": [asdict(c) for c in commits]}

    async def source_read(args: dict[str, Any]) -> dict[str, Any]:
        out = await container.projects.read_source(
            _uuid(args, "project_id"),
            str(args["path"]),
            branch=args.get("branch"),
            start_line=args.get("start_line"),
            end_line=args.get("end_line"),
        )
        return {"found": out is not None, "source": out}

    async def sql_analyze(args: dict[str, Any]) -> dict[str, Any]:
        """Parse SQL text supplied by the caller. Connects to nothing, executes nothing."""
        text = str(args.get("sql", ""))
        if len(text.encode()) > MAX_SQL_BYTES:
            raise DomainError("sql text too large")
        parser = parsers.get("sql")
        if parser is None:
            raise DomainError("no SQL parser available")
        parsed = parser.parse("input.sql", text)
        return {
            "line_count": parsed.line_count,
            "objects": [
                {"name": s.qualified_name, "kind": s.kind, "start": s.start_line, "end": s.end_line}
                for s in parsed.symbols
            ],
            "references": sorted({c.callee for c in parsed.calls}),
            "parse_error": parsed.parse_error,
        }

    async def evidence_ingest(args: dict[str, Any]) -> dict[str, Any]:
        raw = args.get("content_base64")
        data = base64.b64decode(raw, validate=True) if raw else str(args["content"]).encode()
        project_raw = args.get("project_id")
        record = await container.pipeline.ingest_raw(
            data,
            project_id=uuid.UUID(str(project_raw)) if project_raw else None,
            source_kind=SourceKind.TOOL,
            tool_id="evidence_ingest",
            summary=str(args.get("summary", ""))[:500],
            media_type=str(args.get("media_type", "text/plain")),
        )
        return {"evidence_id": str(record.id), "content_hash": record.content_hash}

    for name, fn, desc in (
        ("knowledge_search", knowledge_search, "Hybrid retrieval over knowledge and code."),
        ("context_build", context_build, "Adaptive context pack via the Context Governor."),
        ("project_sync", project_sync, "Incrementally index a registered project."),
        ("impact_analyze", impact_analyze, "Graph-based change impact analysis."),
        ("git_inspect", git_inspect, "Read-only git history."),
        ("source_read", source_read, "Read indexed source lines."),
        ("sql_analyze", sql_analyze, "Parse SQL text (never connects to a database)."),
        ("evidence_ingest", evidence_ingest, "Store a raw tool result as ephemeral evidence."),
    ):
        catalog.register(name, fn, desc)
