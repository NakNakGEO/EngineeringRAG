# Connecting a reasoning model to Engineering Intelligence OS

Engineering OS is **model-agnostic**: the reasoning model is an external client (Claude, Codex,
Hermes, a local model...). It talks to Engineering OS over MCP (preferred) or plain HTTP. No client
or vendor specifics live in domain code - client configuration lives only in these adapter docs.

## The contract (identical for every client)

1. `bootstrap_project(path)` - identify the project; queue an index sync if needed.
2. `get_context(text, project_id, ...)` - **before** changing anything. If `ready_to_act` is false,
   follow `required_expansions` instead of guessing.
3. Do the work *inside the active workflow node* (`get_run_state(workflow_id)` tells you the node,
   allowed capabilities and acceptance criteria).
4. Need to run/inspect something? `request_capability(capability, arguments)`. If the answer is
   `no_provider`, `denied` or `approval_required`, do **not** improvise a tool - tell the user.
5. `request_specialist(goal)` returns the role definitions justified for the task; adopt them
   yourself. One writer, many reviewers.
6. `report_result(workflow_id, node_id, reporter, outputs, evidence)` - acceptance criteria are
   checked by the platform; claims without evidence do not pass.
7. `get_evidence(evidence_id)` retrieves stored evidence; `search_knowledge(query)` searches.

What is **not** available to any client, by design: SQL or database access of any kind, shell or
file primitives, approval decisions, Root Policy changes. SQL for external systems is returned as
*text* for a human to run.

## Endpoints

| Surface | URL (default) | Auth |
|---|---|---|
| MCP (streamable HTTP) | `http://127.0.0.1:8082/mcp` | optional `Authorization: Bearer $EIOS_MCP_TOKEN` |
| HTTP API | `http://127.0.0.1:8000` (OpenAPI at `/docs`) | none for reads; `X-EIOS-Admin-Token` only for human approvals |
| Live events (SSE) | `GET /runs/{run_id}/stream` | `Last-Event-ID` resume |

Ports bind to `127.0.0.1` only. Set `EIOS_MCP_TOKEN` (>= 12 chars) if other local users share the machine.

## Per-client guides
- [Claude-compatible MCP clients](claude.md)
- [Hermes MCP client](hermes.md)
- [Codex / custom HTTP client](codex-http.md)
- [LLM providers used by Engineering OS itself](llm-providers.md)
