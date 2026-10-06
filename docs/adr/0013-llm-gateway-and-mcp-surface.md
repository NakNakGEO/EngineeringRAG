# ADR 0013: LLM gateway and the compact MCP surface

Status: Accepted (Phase 8).

## LLM gateway (`eios_llm`)
- `LLMProvider` protocol + plain-HTTP adapters (OpenAI-compatible, local OpenAI-compatible,
  Anthropic). **No vendor SDKs** anywhere; domain packages never import adapters.
- A provider exists only if configured (model names are explicit - never defaulted or guessed).
  The local adapter refuses non-local hosts by construction.
- Every call passes the Policy Engine (`llm.call`): project-vault data to a remote provider needs
  a human approval; unknown data class is treated as the most sensitive. Calls are budgeted
  (`llm_calls`, `tokens`, `retries`), retried at most 3 times on transient errors only, and
  recorded as `LLM_STARTED/LLM_COMPLETED` events (usage and latency, never prompt text).
- The primary reasoning model is the external client; the gateway serves internal features only.

## MCP surface
Exactly eight tools plus `health`: `bootstrap_project`, `get_context`, `search_knowledge`,
`request_capability`, `request_specialist`, `get_evidence`, `report_result`, `get_run_state`.
Each is a thin validated adapter over the same services as the HTTP API, so policy, routing,
budgets and observability apply identically. A test pins the tool list and asserts no tool or
parameter names a database/shell/approval primitive.

- Errors are returned as `{"error", "code"}` payloads; no tracebacks.
- Results are compact: context items truncated, evidence content size-limited.
- `request_capability` can only execute registered, routable providers; unknown/forbidden/missing
  capabilities return `denied`/`no_provider` with an instruction not to improvise.
- `report_result` can ingest inline evidence, always recorded as `llm`-sourced RAW evidence (never
  claiming to be tool output).
- The MCP server cannot decide approvals or touch Root Policy. Optional bearer token
  (`EIOS_MCP_TOKEN`) guards the endpoint; DNS-rebinding protection stays on; ports bind to loopback.
- HTTP parity for clients without MCP: `/capabilities/invoke`, `/context/build`, `/team/select`,
  `/workflows/{id}/report`, ... (see `docs/integrations`).
