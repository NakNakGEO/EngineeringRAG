# Implementation status

Source of truth: [`architecture/MASTER_PLAN_AND_PROMPTS.md`](architecture/MASTER_PLAN_AND_PROMPTS.md).

| Phase | Status | Notes |
|---|---|---|
| 0 Foundation | Done | uv workspace, compose, Alembic, health, logging, external-DB guard |
| 1 Domain core + observability | Done | runs, EventEnvelope, append-only store, SSE, budgets, policy model |
| 2 Storage + vaults | Done | vaults + DB checks, knowledge/evidence/memory/decisions, pgvector + FTS, blob store, export selection |
| 3 Project intelligence | Done | identity, hardened git, incremental index, symbols, key-based graph, overlays, job queue, runtime container |
| 4 Retrieval + Context Governor | Done | 9 retrievers, fusion/rerank, graph expansion, L0-L4 governor with honest gap, Impact Analyzer, evals vs vector-only |
| 5 Registries | Done | capability/tool/agent/skill registries, manifests (safe YAML), origin trust ceilings, state history, health, capability router, 8 real builtin tools, 16 agents, 7 skills |
| 6 Policy + tool runtime | Done | hash-pinned Root Policy, default-deny engine, single-use human approvals, append-only audit, secrets broker, fail-closed subprocess sandbox, tool runtime, adversarial tests |
| 7 Workflow + agent selection | Done | YAML workflow graph, node runs, checked acceptance criteria, bounded retries/loops, approval gates, one-writer rule, explainable team selector |
| 8 LLM gateway + MCP | Done | vendor-neutral gateway (OpenAI-compatible/local/Anthropic over HTTP), policy-checked, 8-tool MCP surface, optional bearer token, client docs |
| 9 Governance + evaluation | Done | evidence-gated trust, health rules, supersession, contradictions, dependency invalidation + maintenance job, decision ledger lifecycle, golden-case evaluation feeding routing |
| 10 Gap resolver + Workshop | Done | policy-first gap resolver, workshop lifecycle with sandboxed tests, static scan, human-gated tools, generated agents read-only |
| 11 Visual UI | Done | `web/`: 8 views driven by real events, resumable SSE, virtualised lists, bounded memory, nginx same-origin proxy, CI job |
| 12 Portability + hardening | Done | encrypted Default Vault package + fresh-machine restore proof, retention, hash-chained audit export, backup/restore scripts, threat model, migration docs, perf + recovery tests |

Decisions on questions left open by the assessment (Q4-Q6) are recorded as ADRs when they are needed.

## Since phase 12 (dogfooding on this repository)
- **Project content index** ([ADR 0018](adr/0018-project-content-index.md), migration 0010): documents,
  configuration, SQL, migrations and tests are indexed as searchable text chunks
  (`source.project_chunk`; lockfiles keep a stub, secret-bearing files none), retrieved by the new
  `ContentRetriever` (one `file:` candidate per file with the matching line range, ADR/document
  name matching) and used as a fallback to resolve names no symbol carries (`uq_event_run_seq`,
  `ix_job_claim`). `/projects/{id}/coverage` reports `content_files`, `files_with_content`,
  `content_chunks`, `content_coverage`. On the 48-question dogfood set: pack recall 0.684 -> 0.758,
  search@10 0.385 -> 0.459, expected documents missing 31 -> 14 of 35, migrations 6 -> 1 of 7;
  tests and code in the pack got worse (see the ADR's evaluation).
