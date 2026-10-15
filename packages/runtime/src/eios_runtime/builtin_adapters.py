"""The real, in-process implementations behind the builtin tool manifests.

Every adapter takes validated arguments and returns JSON-serialisable output. None of them can
reach an external database or run a program: ``sql_analyze`` parses SQL *text* only, and
``git_inspect`` uses the hardened read-only git layer.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from eios_capability import AdapterCatalog
from eios_domain.errors import DomainError
from eios_domain.knowledge import DecisionCreate, DecisionStatus, MemoryCreate, SourceKind
from eios_domain.project import FileScope
from eios_domain.vault import Vault
from eios_project_intelligence.parsers import ParserRegistry
from eios_retrieval import RetrievalQuery
from eios_runtime import analyzers
from eios_runtime.static_scan import scan_files
from eios_runtime.web_research import WebResearchError, fetch_text, make_host_gate

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

    async def memory_write(args: dict[str, Any]) -> dict[str, Any]:
        """Remember a short fact about a project. Recorded as written by a client, never verified."""
        item = await container.knowledge.add_memory(
            MemoryCreate(
                vault=Vault.PROJECT,
                project_id=_uuid(args, "project_id"),
                scope=str(args.get("scope", "project")),
                content=str(args.get("content", "")),
                tags=[str(t) for t in args.get("tags", [])][:50],
                importance=float(args.get("importance", 0.5)),
                created_by=str(args.get("created_by", "mcp-client"))[:200],
            )
        )
        return {"memory_id": str(item.id), "vault": item.vault.value, "note": "stored as written by a client; not verified"}

    async def decision_record(args: dict[str, Any]) -> dict[str, Any]:
        """Propose a decision. Always PROPOSED: accepting it is a human act."""
        decision = await container.knowledge.record_decision(
            DecisionCreate(
                vault=Vault.PROJECT,
                project_id=_uuid(args, "project_id"),
                question=str(args.get("question", "")),
                selected=str(args.get("selected", "")),
                rationale=str(args.get("rationale", "")),
                alternatives=list(args.get("alternatives", []))[:50],
                evidence_ids=[uuid.UUID(str(e)) for e in args.get("evidence_ids", [])][:50],
                decider=str(args.get("proposer", "mcp-client"))[:200],
                status=DecisionStatus.PROPOSED,
            )
        )
        return {"decision_id": str(decision.id), "status": decision.status.value, "note": "a human accepts or rejects it in the UI"}

    async def web_research(args: dict[str, Any]) -> dict[str, Any]:
        """Read the text of one page on an allowlisted host; the result is stored as RAW evidence."""
        try:
            page = await fetch_text(str(args.get("url", "")), allow_host=make_host_gate(container.policy, container.web))
        except WebResearchError as exc:
            raise DomainError(str(exc)) from exc
        project_raw = args.get("project_id")
        record = await container.pipeline.ingest_raw(
            page["text"].encode(),
            project_id=uuid.UUID(str(project_raw)) if project_raw else None,
            source_kind=SourceKind.TOOL, tool_id="web_research",
            summary=f"{page['final_url']} - {page['title']}"[:500], media_type="text/plain",
        )  # fmt: skip
        limit = max(500, min(int(args.get("max_chars", 12_000)), 50_000))
        return {
            "url": page["final_url"], "title": page["title"], "content_type": page["content_type"],
            "text": page["text"][:limit], "truncated": page["truncated"] or len(page["text"]) > limit,
            "evidence_id": str(record.id), "content_hash": record.content_hash,
        }  # fmt: skip

    async def security_scan(args: dict[str, Any]) -> dict[str, Any]:
        """Static look at indexed files (working copy when there is one): secrets, risky calls, hooks."""
        project_id = _uuid(args, "project_id")
        prefix = str(args.get("path_prefix", "")) or None
        wanted = max(1, min(int(args.get("max_files", 300)), 1000))
        paths: dict[str, None] = {}
        for scope in (FileScope.COMMITTED, FileScope.OVERLAY):  # uncommitted files count too
            for row in await container.projects.list_files(project_id, scope=scope, path_prefix=prefix, limit=wanted):
                paths.setdefault(row.path)
        files: list[tuple[str, bytes]] = []
        for path in list(paths)[:wanted]:
            src = await container.projects.read_source(project_id, path, max_lines=4000)
            if src is not None:  # a deleted file has no source
                files.append((path, src["text"].encode("utf-8", "replace")))
        report = await asyncio.to_thread(scan_files, files)
        report["scanned_files"] = len(files)
        report["note"] = "heuristic: a clean result means nothing obvious, not that the code is safe"
        return report

    def analyzer(fn: Any) -> Any:
        """Wrap a pure analyzer: bad input becomes a clear tool error."""
        async def run(args: dict[str, Any]) -> dict[str, Any]:
            try:
                return fn(args)
            except analyzers.AnalysisError as exc:
                raise DomainError(str(exc)) from exc

        return run

    for name, fn, desc in (
        ("document_parse", analyzer(analyzers.document_parse), "Text and structure of HTML, Markdown, JSON, YAML, CSV or plain text."),
        ("log_analyze", analyzer(analyzers.log_analyze), "Levels, time span, repeated messages and error lines of a log."),
        ("api_contract_analyze", analyzer(analyzers.api_contract_analyze), "OpenAPI summary and breaking changes against a previous version."),
        ("performance_profile", analyzer(analyzers.performance_profile), "Timing statistics from supplied samples or a trace."),
        ("memory_write", memory_write, "Remember a short fact about a project (client-written, unverified)."),
        ("decision_record", decision_record, "Propose a decision for a human to accept or reject."),
        ("web_research", web_research, "Read one page on an allowlisted host as text; stored as RAW evidence."),
        ("security_scan", security_scan, "Static scan of indexed files for secrets, risky calls and install hooks."),
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
