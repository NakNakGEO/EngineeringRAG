# Implementation status

Source of truth: [`architecture/MASTER_PLAN_AND_PROMPTS.md`](architecture/MASTER_PLAN_AND_PROMPTS.md).

| Phase | Status | Notes |
|---|---|---|
| 0 Foundation | Done | uv workspace, compose, Alembic, health, logging, external-DB guard |
| 1 Domain core + observability | Done | runs, EventEnvelope, append-only store, SSE, budgets, policy model |
| 2 Storage + vaults | Done | vaults + DB checks, knowledge/evidence/memory/decisions, pgvector + FTS, blob store, export selection |
| 3 Project intelligence | Done | identity, hardened git, incremental index, symbols, key-based graph, overlays, job queue, runtime container |
| 4 Retrieval + Context Governor | Done | 9 retrievers, fusion/rerank, graph expansion, L0-L4 governor with honest gap, Impact Analyzer, evals vs vector-only |
| 5 Registries | Pending | |
| 6 Policy + tool runtime | Pending | |
| 7 Workflow + agent selection | Pending | |
| 8 LLM gateway + MCP | Pending | |
| 9 Governance + evaluation | Pending | |
| 10 Gap resolver + Workshop | Pending | |
| 11 Visual UI | Pending | |
| 12 Portability + hardening | Pending | |

Decisions on questions left open by the assessment (Q4-Q6) are recorded as ADRs when they are needed.
