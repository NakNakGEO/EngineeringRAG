# Architecture Decision Records

Decisions that shape Engineering Intelligence OS. The source of truth for the overall design is
[`../architecture/MASTER_PLAN_AND_PROMPTS.md`](../architecture/MASTER_PLAN_AND_PROMPTS.md); ADRs record
decisions made while implementing it, and any deviation from it.

Format: Status, Context, Decision, Consequences. An ADR is never edited to change history: supersede it
with a new one.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-python-first.md) | Python-first | Accepted |
| [0002](0002-docker-first.md) | Docker-first deployment | Accepted |
| [0003](0003-modular-monolith.md) | Modular monolith | Accepted |
| [0004](0004-dedicated-postgresql.md) | Dedicated PostgreSQL for Engineering OS only | Accepted |
| [0005](0005-external-database-forbidden.md) | External databases are forbidden (Root Rule) | Accepted |
| [0006](0006-event-contract-and-ordering.md) | Observability event contract and ordering | Accepted |
| [0007](0007-embeddings-vaults-and-trust.md) | Embeddings, vault enforcement and trust ceilings | Accepted |
| [0008](0008-project-intelligence.md) | Project Intelligence - identity, safe git access, incremental index, overlays | Accepted |
| [0009](0009-retrieval-and-context-governor.md) | Adaptive retrieval, the Context Governor and the Impact Analyzer | Accepted |
| [0010](0010-registries-and-capability-routing.md) | Capability, tool, agent and skill registries; capability routing | Accepted |
| [0011](0011-root-policy-and-tool-runtime.md) | Root Policy, Policy Engine, approvals and the tool runtime | Accepted |
| [0012](0012-workflow-graph-and-team-selection.md) | Workflow graph and agent team selection | Accepted |
| [0013](0013-llm-gateway-and-mcp-surface.md) | LLM gateway and the compact MCP surface | Accepted |
| [0014](0014-knowledge-governance-and-evaluation.md) | Knowledge governance, decision ledger and evaluation | Accepted |
| [0015](0015-gap-resolver-and-workshop.md) | Capability Gap Resolver and Workshop | Accepted |
| [0016](0016-web-ui.md) | Web UI (React, TypeScript, Vite) | Accepted |
| [0017](0017-portability-and-hardening.md) | Portability, retention, audit export and hardening | Accepted |
| [0019](0019-readiness-intent-and-ranked-expansion.md) | Readiness follows the question; graph expansion is ranked, not alphabetical | Accepted |
| [0020](0020-call-resolution-and-impact-risk.md) | Call resolution stays inside a language family; impact risk can reach HIGH and CRITICAL | Accepted |
| [0021](0021-local-setup-and-loopback-hardening.md) | One-command local setup, a real smoke test, loopback Host validation | Accepted |
