# ADR 0006: Observability event contract and ordering

Status: Accepted (Phase 1)

## Context
The master plan requires every important action to be observable and the future UI to reconstruct a
complete run from run + events, scale to 100k+ events, and survive reconnects. The plan's event JSON has
no ordering or resume field; timestamps are not a safe cursor (ties, clock skew, concurrent writers).

## Decision
- **`seq`** (additive to the plan's contract): a per-run, gap-free, strictly increasing integer starting
  at 1. It is assigned in the same transaction as the insert by incrementing `platform.run.next_seq`,
  so concurrent writers to one run serialise on that row and cannot produce gaps or duplicates.
  `UNIQUE (run_id, seq)` backs this up. `seq` is the pagination cursor (`after_seq`), the SSE `id`,
  and the resume token (`Last-Event-ID`).
- **`schema_version`** (additive, currently 1) on every event. Changes are additive; consumers must
  ignore event `type` values they do not know. The plan's 29 types are all present, plus a few
  additive types (`APPROVAL_*`, `WORKFLOW_NODE_*`, `CAPABILITY_RESOLVED`, ...) used by later phases.
- **Spans**: `trace_id` is the run's trace; `span_id`/`parent_span_id` form a tree. A span is
  introduced by emitting events under it; `GET /runs/{id}/graph` derives nodes from spans.
- **Append-only**: a database trigger rejects `UPDATE` and `DELETE` on `observability.event`.
  `DELETE` is only allowed in a transaction that sets `eios.retention = on` (used solely by the
  retention job in Phase 12). Runs that have events cannot be deleted (`ON DELETE RESTRICT`).
- **Redaction** happens inside the store, so no producer can bypass it: values under sensitive key names
  and credentials embedded in URLs are masked before persisting or streaming.
- **Streaming** is Server-Sent Events over a database follower: it polls by `seq` (and wakes
  immediately for same-process appends via an in-process hub), delivers bounded batches, sends
  keepalive comments, ends with `event: end` after the run is terminal and drained, and resumes by
  `Last-Event-ID`. Polling by cursor works across processes (API and worker) without new infrastructure.
- **Budgets**: `ExecutionBudget`/`BudgetTracker` live in the pure domain; a budget overrun is itself
  recorded as a `BUDGET_EXCEEDED` (denied) event before the error propagates.

## Consequences
- Appends to a single run are serialised; this is acceptable (one writer per run is the model) and
  avoids a global sequence. Throughput across runs is unaffected.
- Retention/partitioning of the event table is deferred to Phase 12, but the schema (run-scoped keys,
  timestamp index) does not preclude time partitioning.
- `LISTEN/NOTIFY` is a possible later optimisation; the cursor protocol would not change.
