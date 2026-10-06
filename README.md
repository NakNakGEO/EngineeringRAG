# Engineering Intelligence OS

A local-first, model-agnostic engineering layer that sits under any LLM (Codex, Claude, Hermes, local
models, future models): knowledge and memory, project intelligence, adaptive retrieval, capability
routing, workflows, policy and observability. The LLM is the reasoning core; the engineering knowledge
and controls are durable and replaceable-model-proof.

Source of truth for the design: [`docs/architecture/MASTER_PLAN_AND_PROMPTS.md`](docs/architecture/MASTER_PLAN_AND_PROMPTS.md).
Decisions made while building it: [`docs/adr/`](docs/adr/README.md).

## Status

All twelve phases of the master plan are implemented (see
[`docs/implementation-status.md`](docs/implementation-status.md) for what each phase delivered and
[`docs/adr/`](docs/adr/README.md) for the decisions):

- **Knowledge & memory** with Default / Project / Ephemeral vaults, evidence-gated trust, health,
  supersession, contradictions, dependency invalidation, a decision ledger.
- **Project intelligence**: hardened read-only git, incremental indexing, symbol/dependency graph.
- **Adaptive retrieval** and a Context Governor that reports honestly what it does not know.
- **Capability routing**: registries of capabilities, tools, agents, skills; never guesses; gap
  resolver and a human-gated Workshop for generated skills/agents/tools.
- **Policy**: immutable hash-pinned Root Policy, default-deny engine, single-use human approvals,
  append-only audit, fail-closed sandbox. **No path to any external database.**
- **Workflows** (UNDERSTAND -> DESIGN -> IMPLEMENT -> VERIFY -> CURATE) with checked acceptance
  criteria and one-writer-many-reviewers.
- **Model-agnostic access**: an 8-tool MCP server and HTTP API ([`docs/integrations`](docs/integrations/README.md)),
  plus a vendor-neutral LLM gateway.
- **Live UI** (`web/`, port 8080) driven by real events, **portable encrypted export**, retention,
  hash-chained audit export, [threat model](docs/security/threat-model.md).

## Quick start

Requirements: Docker (with Compose), and for development [uv](https://docs.astral.sh/uv/).

```bash
make init          # creates .env from .env.example - review/edit the password
make up            # build and start everything, waits until healthy
curl localhost:8000/health/ready
make down
```

| Service | Where | Health |
|---|---|---|
| API | http://127.0.0.1:8000 (`/docs`) | `/health/live`, `/health/ready` |
| MCP server | http://127.0.0.1:8082/mcp (streamable HTTP) | `/health/live`, `/health/ready` |
| Web UI | http://127.0.0.1:8080 | served by nginx, proxies `/api` |
| Worker | internal only (port 8081 in the container) | `/health/live`, `/health/ready` |
| PostgreSQL | 127.0.0.1:5432 (loopback only) | `pg_isready` |

Development workflow, tests, migrations and rollback: [`docs/development.md`](docs/development.md).

## Non-negotiable rules (summary)

Python-first, Docker-first, local-first. PostgreSQL belongs exclusively to Engineering OS and there is
**no connection to any external, company, QA or production database**: for those, the system only
generates SQL for a human to run. Root Policy cannot be modified by LLMs, agents, skills, tools,
plugins or generated code. See [`AGENTS.md`](AGENTS.md).

## Repository layout

```
apps/            api, worker, mcp_server        (entry points)
packages/        core, domain, storage, observability, knowledge, project_intelligence, retrieval,
                 capability, policy, workflow, governance, workshop, llm, portability, runtime
web/             React + TypeScript UI
manifests/ workflows/ evals/ policy/   data: capabilities, tools, agents, skills; workflow graphs; golden cases; Root Policy
migrations/      Alembic environment and revisions
scripts/         repo tooling (forbidden-dependency check)
tests/           unit, integration (real PostgreSQL), security
docs/            architecture, ADRs, development guide
```
