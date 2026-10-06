# ADR 0004: Dedicated PostgreSQL for Engineering OS only

Status: Accepted (Phase 0)

## Context
PostgreSQL is the authoritative store (relational data, full-text search, pgvector, DB-backed job
queue). Engineering OS must have full control of its database and must be isolated from any other.

## Decision
Engineering OS owns exactly one PostgreSQL instance: the `postgres` compose service, image
`pgvector/pgvector:pg16` (PostgreSQL 16 with pgvector available; the extension is enabled in Phase 2,
not before). Data lives in a named Docker volume. It is the only database any component is configured
for: there is a single `EIOS_DATABASE_URL` setting and no setting for anything else.

- Alembic manages the schema; revisions live in `migrations/`. The database URL is never written in
  `alembic.ini`; `migrations/env.py` reads `Settings`, so migrations go through the same policy as the
  application.
- Each later phase adds the migrations (and PostgreSQL schema) it owns. Phase 0 ships an empty
  baseline revision (`0001`) to exercise the chain, `upgrade`, `downgrade` and offline SQL.
- Tests run against throwaway databases (`eios_test_<random>`) created and dropped on the same
  dedicated server; they never touch the development database.
- The Postgres port is published on `127.0.0.1` only, for host-run tools and tests.

## Consequences
- Background jobs can use PostgreSQL leases (`FOR UPDATE SKIP LOCKED`) behind an interface later.
- Backups, retention and export/import are Phase 12 concerns.
