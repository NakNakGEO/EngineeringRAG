# ADR 0005: External databases are forbidden (Root Rule)

Status: Accepted (Phase 0). Part of Root Policy; cannot be overridden by an LLM, agent, tool, skill,
workflow, plugin, generated capability or subsystem.

## Context
Engineering OS must never connect to, read, write, execute against, or alter an external, company, QA
or production database, nor store credentials for one. For external database work it only analyses
files and text and generates SQL for a human to execute manually.

## Decision
Enforce it structurally, in layers, starting in Phase 0. Prompt wording alone is not a control.

1. **Runtime allowlist** (`eios_core.database_policy`). Every database URL is validated before any
   engine exists. Only driver `postgresql+psycopg` and hosts `postgres`, `localhost`, `127.0.0.1`,
   `::1` pass. Everything else is rejected: other products and drivers, other hosts and lookalikes,
   multi-host URLs, unix sockets/empty host, userinfo tricks, and libpq parameters that can redirect
   a connection (`host`, `hostaddr`, `service`, `passfile`, ...). The allowlist is a code constant:
   not configurable by environment, settings, API, plugin or agent. Errors never echo the URL.
2. **Single entry point.** Only `eios_storage` creates engines (`build_async_engine`,
   `build_sync_engine`), and both re-check the policy. Alembic uses the same factory.
3. **Configuration surface.** `Settings` has one database setting, validated by the policy. Neither
   `.env.example`, `alembic.ini` nor `docker-compose.yml` carries any other database address.
4. **Supply chain** (`scripts/check_forbidden_deps.py`, run by `make check` and CI). Fails if any
   external-database driver (SQL Server, MySQL, Oracle, other PostgreSQL drivers, warehouses, NoSQL)
   appears in any `pyproject.toml`, in `uv.lock` (including transitive dependencies), or as an import
   in source. `psycopg` 3 is the one allowed driver. The same check blocks the technologies the master
   plan excludes from V1 (Redis, Kafka, Neo4j, Qdrant, Kubernetes).
5. **Adversarial tests** (`tests/security`) for all of the above.

Planned later, not part of Phase 0: manifest-level rejection of `external_database_*` capabilities
(Phase 5/6), default-deny network for tool sandboxes, and a secrets broker that refuses
database-connection secrets (Phase 6).

## Consequences
- "Impossible through the supported runtime" is what is guaranteed. A person running arbitrary code on
  their own machine is outside this model; Phase 6 narrows that further with sandbox network policy.
- The localhost allowlist means any local PostgreSQL on those hosts passes. The target is expected to
  be the compose database; Phase 6 may additionally pin the database identity.
- Adding a database host or driver requires a code change, a new ADR and review.
