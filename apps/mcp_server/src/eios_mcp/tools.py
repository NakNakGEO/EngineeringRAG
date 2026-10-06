"""The compact MCP tool surface: eight tools, nothing more.

Design rules (master plan, Phase 8):

* the LLM sees few, high-level tools - not SQL, not files, not shells;
* every tool is a thin, validated adapter over the same services the HTTP API uses, so policy,
  budgets, routing and observability apply identically;
* arguments are validated; errors are returned as ``{"error": ...}`` payloads, never tracebacks;
* nothing here can reach a database, execute a program, change Root Policy or decide an approval.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import ValidationError

from eios_capability import RoutingRequest
from eios_domain.errors import DomainError, NotFoundError
from eios_domain.events import ActorType
from eios_domain.knowledge import SourceKind
from eios_domain.policy import Risk
from eios_domain.project import BootstrapState
from eios_policy import ToolInvocation
from eios_project_intelligence import WorkspaceViolationError
from eios_retrieval import ContextLevel, RetrievalQuery
from eios_runtime import Container
from eios_workflow import TeamSignals, WorkflowError, select_team

TOOL_NAMES = (
    "bootstrap_project",
    "get_context",
    "search_knowledge",
    "request_capability",
    "request_specialist",
    "get_evidence",
    "report_result",
    "get_run_state",
)
ITEM_CHARS = 1500
MAX_EVIDENCE_CHARS = 20_000
MAX_INLINE_EVIDENCE = 10


def err(message: str, **extra: Any) -> dict[str, Any]:
    return {"error": message, **extra}


def _uuid(value: str | None, name: str) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise DomainError(f"'{name}' must be a UUID") from exc


def _compact_item(item: Any) -> dict[str, Any]:
    c = item.candidate
    content = c.content or ""
    return {
        "id": c.id,
        "kind": c.kind.value if hasattr(c.kind, "value") else str(c.kind),
        "title": c.title,
        "path": c.path,
        "lines": [c.start_line, c.end_line] if c.start_line else None,
        "text": content[:ITEM_CHARS] + ("..." if len(content) > ITEM_CHARS else ""),
        "score": round(item.score, 3),
        "critical": item.critical,
        "why": item.reasons[:3],
        "trust": c.trust.value if c.trust else None,
        "health": c.health.value if c.health else None,
    }


async def bootstrap_project(c: Container, path: str, sync: str = "if_needed") -> dict[str, Any]:
    if sync not in {"never", "if_needed", "always"}:
        return err("sync must be never, if_needed or always")
    try:
        result = await c.projects.bootstrap(path)
    except WorkspaceViolationError as exc:
        return err(str(exc), code="outside_approved_workspace")
    out: dict[str, Any] = {
        "state": result.state.value,
        "project_id": str(result.project.id) if result.project else None,
        "branch": result.branch,
        "head": result.head,
        "dirty_paths": result.dirty_paths,
        "needs_sync": result.needs_sync,
        "detail": result.detail,
    }
    wanted = sync == "always" or (sync == "if_needed" and result.needs_sync)
    if result.project is not None and result.state is not BootstrapState.ERROR and wanted:
        job, run_id = await c.projects.request_sync(result.project.id)
        out.update(sync_queued=True, run_id=str(run_id), job_id=str(job.id))
    return out


async def get_context(
    c: Container,
    text: str,
    project_id: str | None = None,
    symbols: list[str] | None = None,
    paths: list[str] | None = None,
    risk: str = "medium",
    token_budget: int = 12_000,
    max_level: str = "L4",
) -> dict[str, Any]:
    try:
        query = RetrievalQuery(
            text=text, project_id=_uuid(project_id, "project_id"), symbols=symbols or [],
            paths=paths or [], risk=Risk(risk),
        )  # fmt: skip
        level = ContextLevel(max_level)
    except (ValidationError, ValueError, DomainError) as exc:
        return err(f"invalid arguments: {str(exc)[:200]}")
    budget = max(500, min(token_budget, 200_000))
    try:
        async with c.recorder.run(
            kind="context_build", goal=text[:500], project_id=query.project_id
        ) as ctx:
            pack = await c.governor.build(query, token_budget=budget, max_level=level, ctx=ctx)
    except NotFoundError as exc:
        return err(str(exc), code="not_found")
    return {
        "run_id": str(ctx.run_id),
        "level": pack.level.value,
        "ready_to_act": pack.gap.ready_to_act,
        "confidence": round(pack.gap.confidence, 3),
        "coverage": round(pack.gap.coverage, 3),
        "missing_context": [m.model_dump() for m in pack.gap.missing_context],
        "required_expansions": [a.model_dump() for a in pack.required_expansions],
        "contradictions": pack.contradictions,
        "items": [_compact_item(i) for i in pack.items],
        "used_tokens": pack.used_tokens,
        "truncated": pack.truncated,
        "reason": pack.reason,
    }


async def search_knowledge(
    c: Container, query: str, project_id: str | None = None, limit: int = 10
) -> dict[str, Any]:
    try:
        q = RetrievalQuery(text=query, project_id=_uuid(project_id, "project_id"))
    except (ValidationError, DomainError) as exc:
        return err(f"invalid arguments: {str(exc)[:200]}")
    try:
        result = await c.governor.search(q, limit=max(1, min(limit, 50)))
    except NotFoundError as exc:
        return err(str(exc), code="not_found")
    return {
        "items": [_compact_item(i) for i in result.items[: max(1, min(limit, 50))]],
        "errors": result.errors,
    }


async def request_capability(
    c: Container,
    capability: str,
    arguments: dict[str, Any] | None = None,
    run_id: str | None = None,
    language: str | None = None,
    extension: str | None = None,
    kind: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Ask for a capability by name. Routing, policy and approvals happen server-side."""
    try:
        rid = _uuid(run_id, "run_id")
    except DomainError as exc:
        return err(str(exc))
    ctx = await c.recorder.resume(rid) if rid else None
    call = ToolInvocation(
        capability=capability.strip(),
        arguments=arguments or {},
        actor_type=ActorType.LLM,
        actor_id="mcp_client",
        routing=RoutingRequest(
            capability=capability.strip(), language=language, extension=extension, kind=kind,
            path=path,
        ),
    )  # fmt: skip
    result = await c.tools.invoke(call, ctx)
    out = result.summary()
    if result.status == "no_provider":
        out["next"] = (
            "No registered provider can do this. Do not improvise a tool; ask the user whether the "
            "Capability Workshop should propose one (a human approves any executable)."
        )
    if result.status == "approval_required":
        out["next"] = "A human must approve this in the Engineering OS approval queue; then retry."
    return out


async def request_specialist(
    c: Container,
    goal: str,
    role: str | None = None,
    risk: str = "medium",
    paths: list[str] | None = None,
    touches_data_layer: bool = False,
    tests_missing: bool = False,
) -> dict[str, Any]:
    """Return the specialist role definition(s) to adopt - the reasoning stays with the client."""
    from eios_runtime.container import _agent_infos

    try:
        rk = Risk(risk)
    except ValueError:
        return err("risk must be low, medium, high or critical")
    agents = await _agent_infos(c.registry.store)
    if role is not None:
        info = agents.get(role)
        if info is None or not info.routable:
            return err(f"role '{role}' is not registered or not routable", code="unknown_role")
        roles, reasons, team = [role], {role: "requested explicitly"}, None
    else:
        team = select_team(
            goal,
            TeamSignals(risk=rk, paths=tuple(paths or []), touches_sql=touches_data_layer,
                        tests_missing=tests_missing, explicit_targets=bool(paths)),
            agents,
        )  # fmt: skip
        roles, reasons = team.roles, team.reasons
    definitions = []
    for r in roles:
        row = await c.registry.store.get_agent(r)
        if row is None:
            continue
        m = row["manifest"]
        definitions.append(
            {"role": r, "reason": reasons.get(r, ""), "can_write": bool(row["can_write"]),
             "prompt": m["prompt"], "capabilities": m["capabilities"],
             "forbidden_actions": m["forbidden_actions"],
             "review_requirements": m["review_requirements"]}
        )  # fmt: skip
    return {
        "team": team.model_dump(mode="json") if team else None,
        "definitions": definitions,
        "note": "Adopt the role prompt yourself; only the selected writer may change files.",
    }


async def get_evidence(c: Container, evidence_id: str, max_chars: int = 8000) -> dict[str, Any]:
    try:
        eid = _uuid(evidence_id, "evidence_id")
    except DomainError as exc:
        return err(str(exc))
    assert eid is not None  # noqa: S101 - evidence_id is required and non-empty here
    record = await c.evidence.get(eid)
    if record is None:
        return err("evidence not found", code="not_found")
    out: dict[str, Any] = {
        "id": str(record.id), "summary": record.summary, "tool_id": record.tool_id,
        "source_kind": record.source_kind.value, "trust": record.trust.value,
        "media_type": record.media_type, "size_bytes": record.size_bytes,
        "content_hash": record.content_hash, "vault": record.vault.value,
        "project_id": str(record.project_id) if record.project_id else None,
        "created_at": record.created_at.isoformat(),
    }  # fmt: skip
    limit = max(100, min(max_chars, MAX_EVIDENCE_CHARS))
    if record.media_type.startswith("text/") or record.media_type == "application/json":
        sha = await c.evidence.blob_sha(eid)
        try:
            data = c.blobs.get(sha) if sha else None
        except (OSError, DomainError):
            data = None
        if data is not None:
            text = data.decode(errors="replace")
            out["content"] = text[:limit]
            out["truncated"] = len(text) > limit
    return out


async def report_result(
    c: Container,
    workflow_id: str,
    node_id: str,
    reporter: str,
    outputs: dict[str, Any] | None = None,
    evidence_ids: list[str] | None = None,
    evidence: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Report a node's outcome. Inline ``evidence`` is ingested as RAW, LLM-sourced evidence."""
    try:
        wid = _uuid(workflow_id, "workflow_id")
        assert wid is not None  # noqa: S101
        ids = [e for e in (_uuid(x, "evidence_ids") for x in evidence_ids or []) if e]
    except DomainError as exc:
        return err(str(exc))
    if len(evidence or []) > MAX_INLINE_EVIDENCE:
        return err(f"at most {MAX_INLINE_EVIDENCE} inline evidence items per report")
    try:
        run = await c.workflows.get(wid)
        if run is None:
            return err("workflow not found", code="not_found")
        for item in evidence or []:
            record = await c.pipeline.ingest_raw(
                str(item.get("content", "")).encode(),
                project_id=run.project_id,
                source_kind=SourceKind.LLM,  # never claims to be tool output
                tool_id=str(item.get("label", "client_reported"))[:100],
                summary=str(item.get("summary", ""))[:500],
                media_type="text/plain",
            )
            ids.append(record.id)
        await c.workflows.report(
            wid, node_id=node_id, reporter=reporter, outputs=outputs or {}, evidence_ids=ids
        )
        return await c.workflows.brief(wid)
    except WorkflowError as exc:
        return err(str(exc), code=exc.kind)
    except DomainError as exc:
        return err(str(exc))


async def get_run_state(
    c: Container, run_id: str | None = None, workflow_id: str | None = None, events: int = 15
) -> dict[str, Any]:
    try:
        rid, wid = _uuid(run_id, "run_id"), _uuid(workflow_id, "workflow_id")
    except DomainError as exc:
        return err(str(exc))
    out: dict[str, Any] = {}
    if wid is not None:
        try:
            brief = await c.workflows.brief(wid)
        except WorkflowError as exc:
            return err(str(exc), code=exc.kind)
        out["workflow"] = brief
        rid = rid or uuid.UUID(brief["run_id"])
    if rid is None:
        return err("give run_id or workflow_id")
    run = await c.runs.get(rid)
    if run is None:
        return err("run not found", code="not_found")
    page = await c.events.list_events(rid, limit=500)
    recent = page.items[-max(1, min(events, 50)) :]
    out["run"] = {
        "id": str(run.id), "kind": run.kind, "status": run.status.value, "goal": run.goal[:300],
        "error": run.error,
    }  # fmt: skip
    out["events"] = [
        {"seq": e.seq, "type": e.type, "status": e.status.value, "summary": e.summary}
        for e in recent
    ]
    return out
