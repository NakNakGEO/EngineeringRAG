# ADR 0010: Capability, tool, agent and skill registries; capability routing

Status: Accepted (Phase 5).

## Model
- **Capability** = a semantic need (`sql_analyze`, `file_edit`). **Provider** (tool/adapter/subsystem)
  implements capabilities. **Agent** = reasoning role. **Skill** = reusable procedure.
- Everything is YAML data, parsed with `yaml.safe_load`, validated by strict Pydantic models
  (unknown fields rejected, sizes bounded, symlinks ignored) and stored in PostgreSQL with
  versions and a state history.
- States: `UNREGISTERED, EXPERIMENTAL, VERIFIED, TRUSTED, DISABLED, BROKEN, QUARANTINED`.
  Only the first three routable states can be routed.

## Rules (all enforced in code and, where cheap, in DB CHECKs)
- Forbidden capabilities (`external_database_*`) are rejected at manifest validation and by a DB
  CHECK; the router answers `FORBIDDEN` for them. The same list becomes part of Root Policy (Phase 6).
- A manifest cannot claim more trust than its **origin** allows: only `builtin` may be TRUSTED;
  plugin/generated/downloaded are capped at EXPERIMENTAL.
- Generated/downloaded artifacts are not routable until a human approved them; writer agents from
  non-builtin origins are registered DISABLED until a human decides.
- Versions of non-builtin artifacts are immutable; changed content needs a new version.
- Transitions: demotion always allowed; EXPERIMENTAL->VERIFIED needs passed verification;
  ->TRUSTED and leaving QUARANTINED need a human; every change is recorded in `state_history`.
  A re-sync never resets a demotion.
- A provider is only as real as the code behind it: `AdapterCatalog` holds the in-process
  implementations; a manifest without one is health-checked `fail` and never routed.
  Health checks never execute providers.

## Routing
`CapabilityRouter.resolve` filters on state/approval/health/target/network, ranks by state, trust,
maturity, priority, target specificity and observed success/latency/eval, and returns an
explanation. It **never guesses**: unknown capability, or no compatible provider, yields an
explicit non-selection (and a `CAPABILITY_GAP_DETECTED` event) that feeds the Gap Resolver (Phase 10).

## LLM-facing surface
Compact summaries by default (`GET /capabilities`, `/tools`, `/agents`, `/skills`); full detail
is fetched lazily (`/capabilities/{id}` ...). Builtin: 25 capabilities, 8 real tools, 16 agents,
7 skills. Capabilities with no provider (e.g. `file_edit`, `test_run`) are intentionally listed:
they require the sandboxed tool runtime (Phase 6) and show up as honest gaps until then.
