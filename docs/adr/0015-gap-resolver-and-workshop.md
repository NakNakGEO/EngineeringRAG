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

## Amendment: gap events and intent screening (dogfood finding 14)
- **Event status.** The gap resolver's event no longer says `failed` for normal outcomes. RESOLVED,
  COMPOSABLE, GENERATABLE and EXTERNAL_REQUIRED are `completed`; BLOCKED_BY_POLICY is `denied`;
  only IMPOSSIBLE is `failed`. `data.outcome` carries the gap status.
- **Detector only widens.** `detects_external_database_intent` still runs the original pattern on
  the raw text, then a wider one on normalised words (identifiers split into words, Unicode
  normalised, zero-width characters and Cyrillic/Greek look-alikes removed): verbs (read, fetch,
  pull, query, connect, ...) reaching a database, a vendor (Oracle, SQL Server, Redshift, ...) or an
  environment-qualified target (production, prod, QA, company, ...), including SQL run "against
  prod", database command-line clients, and connector-like noun phrases (`external_db_connector`,
  `oracle_reader`). "As text / for a human" still exempts, but only within its own clause and not
  when the clause says it is unattended ("directly", "automatically"). A database noun that only
  modifies a file-like noun ("database migration files") is not a target for the new patterns.
  Over-long requests fail closed. Known cost: the original pattern's false positives remain
  (e.g. "read from the database migration files", "run tests on the database").
- **Names.** `is_forbidden_capability` also matches `external_db*` / `external_sql*` prefixes and
  token rules independent of casing, separators and order (`oracle_reader`, `run_sql`,
  `modify_root_policy`). The widened prefixes are merged into the loaded Root Policy like the
  existing compiled-in ones; `policy/root_policy.yaml` and its lock are unchanged.
- **Goals.** `/team/select` and `POST /workflows` screen the goal. A match forces risk `critical`
  (so every human gate applies), audits a `root.external_database` denial on the run, and adds a
  `policy_notice` with the reason to the response. The request is not refused: it proceeds with
  maximum review, never silently.
