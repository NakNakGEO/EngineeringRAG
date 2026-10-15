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

### Who may report a node (`NodeDef.roles`)
`roles` lists who may report a node; empty means any team member. Entries are agent role ids or
the keywords `reviewer` (any team member except the writer) and `writer`. A role a node names may
report even if the selector did not put it on the team (the CURATE node names `knowledge_curator`),
provided it is registered and routable. In `engineering.default` VERIFY is `[reviewer]` and CURATE
is `[knowledge_curator, writer]`.

A VERIFY report from the writer while a non-writer teammate exists is rejected with the
`self_review` error (other roles: `role_not_allowed`), a `POLICY_DENIED` event
(`independent_review` / `node_roles`) and no state change - the attempt is not consumed. A team
with no non-writer member (a low-risk docs edit) cannot be independent; the self-review is then
allowed and recorded as a waived `POLICY_ALLOWED` event. The `reporter` string is declared by the
client, so this is a process control that makes the audit trail honest, not authentication.

## Signals derived from the project
`start()` takes the caller's `TeamSignals` and, when `project_id` and `paths`/`symbols` are given,
asks an injected `FactsProvider` (the runtime container wires `ImpactAnalyzer` + semantic
coverage; the workflow package imports neither) for dependents, missing tests, SQL involvement and
coverage. Facts only raise what the caller stated: `risk = max(caller, impact)`, flags are OR-ed,
`dependents` is the maximum. Documentation-only paths are not analysed (nothing depends on a
README, and "no tests found" would be noise), and an analysis failure never blocks the start - it
is recorded as `impact_error`. The effective signals and an impact summary are stored in
`TeamSelection.signals` (inside the existing `team` JSON column, no schema change), emitted with
`GOAL_CLASSIFIED` and returned by `brief()`.

## Definitions chosen by the engine
`start(definition_id=None)` selects `engineering.docs` (UNDERSTAND -> IMPLEMENT -> VERIFY ->
CURATE, no evidence criterion) when the primary role is the technical writer and the effective
risk is low, otherwise `engineering.default`. An explicit `definition_id` always wins. Docs work
has no test/build/lint output; requiring it only made agents invent evidence.

## The brief names the exact output keys
`brief().node.criteria` lists `id, description, kind, field, value, allow_empty, min_count` (plus
`source_tool` / `capability` when set), `reporters` (who may report) and `roles`. For `field`
criteria the id equals the output key (`context_confidence`, `reuse_candidates`); a test enforces
this for every shipped definition, so a first report no longer burns an attempt on a guessed key.

## Team selector
Deterministic and explainable (`TeamSelection`: primary, specialists, reasons per role,
capabilities needed, review requirements, confidence, and *skipped roles with reasons*).

Goals are matched on identifier-split tokens (`eios_knowledge.text.tokenize`), so `admin_token`,
`_require_admin` and `PolicyEngine.evaluate` are read as words. A name the task introduces
("add a new `SANDBOX_TIMEOUT` EventType", "a `created_by` field") does not trigger subject
keywords. Paths named by the caller map to roles by glob (policy/auth/admin routes/sandbox ->
security; migrations/tables/`*.sql` -> database; docker/compose/CI -> devops). `table`, `model`
and `module` no longer trigger a role on their own (database / ML / architect need a
co-occurring term). Documentation-only work ignores subject keywords and path roles: the words
of a README describe its topic, not the change.

A change verb counts only as the main verb of a clause ("fix X and update Y"), so the noun
"writer" or "builds" no longer makes a read-only goal a coding task. Read-only goals (review,
audit, explain, investigate...) get a non-writer primary (code reviewer, architect or project
archaeologist) and `file_edit` / `build_run` are dropped from the needed capabilities.

Impact signals: risk, SQL touched, dependents, missing tests, coverage. Specialists are capped
(2; 4 at critical risk), prioritised, and only roles that are registered and routable can be
selected - an unavailable needed role is reported and lowers confidence rather than silently
ignored. Confidence rises with evidence-backed triggers (goal wording, paths, impact) and falls
for specialists staffed only because of the risk label.

## Execution graph
A workflow run has one root span (its id is kept in the run metadata; `RunRecorder.resume()`
re-attaches to it, so report/resume/cancel and worker calls no longer start new roots). Every node
attempt is a child span whose id is the `NodeRun` id: `WORKFLOW_NODE_STARTED` and
`WORKFLOW_NODE_COMPLETED` share it and a failed attempt keeps its own `failed` status. Policy
decisions (a pending approval, a rejected report) are their own child spans, so an open attempt
stays `started`. `build_graph` labels node spans `STAGE/node #attempt` and takes a span's status
from its closing event (a failure, a denial or a `*_COMPLETED` event) instead of the latest event.

## Known limits
Execution-governor token/LLM budgets are per-process (`RunContext`); only step count and attempts
are persisted. The selector is heuristic; an LLM may propose a team through `request_specialist`
(Phase 8) but the engine still enforces the one-writer and availability rules.
