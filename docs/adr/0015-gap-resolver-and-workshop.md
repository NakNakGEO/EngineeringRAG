# ADR 0015: Capability Gap Resolver and Workshop

Status: Accepted (Phase 10).

## Gap Resolver
Order: policy check -> existing capability (by name, then a conservative semantic match) ->
compose existing capabilities -> generate a skill -> generate an agent -> generate a tool ->
external subsystem. Statuses: RESOLVED, COMPOSABLE, GENERATABLE, EXTERNAL_REQUIRED, IMPOSSIBLE,
BLOCKED_BY_POLICY. The policy check comes first: forbidden capability names (`external_database_*`,
`root_policy*`, `policy_modify*`) and needs that read as live database access
(`detects_external_database_intent`, advisory - the structural defences remain) are
BLOCKED_BY_POLICY, audited, and never reach generation. A need that names several capabilities is
a composition, not a single match. Privileged-host needs are IMPOSSIBLE; running external services
are EXTERNAL_REQUIRED (a human decides; never generated).

## Workshop lifecycle
DRAFT -> SANDBOXED -> TESTED -> EXPERIMENTAL -> VERIFIED -> TRUSTED (or REJECTED).
- Proposals store version, creator, prompt hash, need, capability claims, tests, results, scan
  report and approval; content is immutable after DRAFT (changes need a new version).
- Skills/agents: structurally tested automatically, then registered EXPERIMENTAL without a human.
  Agents are never writers and automatically inherit the Root Policy forbidden actions.
- Tools: static scan (no network/process/db/dynamic-exec/policy references), hash-pinned launcher,
  tests run in the sandbox, **human approval to register**, >= 3 tests plus a human for VERIFIED,
  TRUSTED human-only. Generated tools may request no network, secrets or filesystem access.
  Tampering after approval is caught by the pinned hash at run time.
- Root Policy cannot be generated or modified: such capability names are forbidden prefixes and
  proposal kinds are limited to skill/agent/tool.
- Scan is defence in depth only; the sandbox and Policy Engine are the real boundary.

## Surface
HTTP: `/gaps/resolve`, `/workshop/proposals[/...]`; human steps need the admin token. MCP
`request_capability` attaches the gap resolution to `no_provider` answers.
