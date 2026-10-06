# Engineering Intelligence OS

A local-first, model-agnostic engineering layer that sits under any LLM (Codex, Claude, Hermes, local
models, future models): knowledge and memory, project intelligence, adaptive retrieval, capability
routing, workflows, policy and observability. The LLM is the reasoning core; the engineering knowledge
and controls are durable and replaceable-model-proof.

Source of truth for the design: [`docs/architecture/MASTER_PLAN_AND_PROMPTS.md`](docs/architecture/MASTER_PLAN_AND_PROMPTS.md).
Decisions made while building it: [`docs/adr/`](docs/adr/README.md).

## Status

**Phase 0 (Foundation) is complete.** Nothing from Phase 1+ exists yet. What works today:

- `docker compose up` boots PostgreSQL, a migration step, the API, the worker and the MCP server,
  each with a health endpoint.
- Structured JSON logging with correlation IDs, typed settings, Alembic migrations.
- Root Rule scaffolding: Engineering OS can only be configured to use its own PostgreSQL
  ([ADR 0005](docs/adr/0005-external-database-forbidden.md)).

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
packages/        core, storage                  (libraries; more arrive per phase)
migrations/      Alembic environment and revisions
scripts/         repo tooling (forbidden-dependency check)
tests/           unit, integration (real PostgreSQL), security
docs/            architecture, ADRs, development guide
```
