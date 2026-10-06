# ADR 0012: Workflow graph and agent team selection

Status: Accepted (Phase 7).

## Macro-deterministic, micro-agentic
The reasoning model is an *external* client (Claude, Codex, Hermes, a local model...). The engine
never calls a model. It tells the client where the run is (`brief`: node, allowed capabilities,
acceptance criteria, attempts left, last failure), and the client reports results. The graph owns
transitions, retries, approvals, budgets and completion; the model owns the work inside a node.

## Definitions
`workflows/*.yaml` (data, validated: reachable nodes, defined targets, a terminal node, bounded
retries). A run snapshots its definition so later edits never change a running workflow.
Default `engineering.default`: UNDERSTAND -> DESIGN -> IMPLEMENT -> VERIFY -> CURATE, with
VERIFY failure looping back to IMPLEMENT.

## Acceptance criteria are checked, not believed
Criteria are deterministic: `field`, `equals`, `evidence` (referenced evidence records must exist,
belong to the same project and optionally come from a given tool - unknown or foreign ids do not
count) and `capability` (a completed tool call is recorded on the run). A claim like
`all_passed: true` without evidence fails VERIFY.

## Retries, loops and budgets
Per-node attempt counters are cumulative across loops, so VERIFY -> IMPLEMENT -> VERIFY ends;
`max_total_steps` bounds the whole run (`BUDGET_EXCEEDED` event, workflow FAILED).
State changes use optimistic concurrency (`version`); a stale writer gets a `conflict`.

## Approval gates
A node with a `gate` waits (`waiting_approval`) when the run risk meets the threshold. The gate
uses the Policy Engine (`workflow.gate` -> REQUIRE_APPROVAL, single-use human approval). Reports
are rejected while waiting; `resume` consumes an approval; a denied approval never proceeds.

## One writer, many reviewers
The selector picks exactly one writer (or none for read-only goals). A report that changes files
from any other role, or outside a writer node, is rejected with a `POLICY_DENIED` event and changes
no state. Reports from roles that are not on the team are rejected.

## Team selector
Deterministic and explainable (`TeamSelection`: primary, specialists, reasons per role,
capabilities needed, review requirements, confidence, and *skipped roles with reasons*).
Whole-word keyword triggers plus impact signals (risk, SQL touched, dependents, missing tests,
coverage). Specialists are capped (3; 4 at critical risk), prioritised, and only roles that are
registered and routable can be selected - an unavailable needed role is reported and lowers
confidence rather than silently ignored.

## Known limits
Execution-governor token/LLM budgets are per-process (`RunContext`); only step count and attempts
are persisted. The selector is heuristic; an LLM may propose a team through `request_specialist`
(Phase 8) but the engine still enforces the one-writer and availability rules.
