# ADR 0003: Modular monolith

Status: Accepted (Phase 0)

## Context
The master plan requires a modular monolith first and forbids splitting services "merely because
future scale is imaginable". It also requires the domain to be free of FastAPI, vendor LLM SDKs, Docker
SDK and vendor tool SDKs.

## Decision
A uv workspace of small packages with one-way dependencies, in the layout of the master plan:

```
apps/*        entry points (api, worker, mcp_server)   -> may use packages/*
packages/*    libraries (core, storage, ...)           -> never import apps/*
```

Phase 0 creates only the packages it needs: `core` (settings, logging, correlation IDs, health models,
database policy) and `storage` (engine factories, health probe, Alembic metadata). Later phases add
their own packages (`domain`, `observability`, `policy`, `retrieval`, ...) when they have real content;
empty placeholder packages are not created.

Rules: `core` depends on neither web frameworks nor database drivers; only `storage` creates database
engines; web/MCP frameworks stay in `apps/*`; shared behaviour is injected (explicit dependency
injection, no global mutable registries).

`api`, `worker` and `mcp` are separate processes sharing one codebase and one database. Splitting them
further is not planned.

## Consequences
- Package boundaries are enforced by each package's declared dependencies, not by convention.
- Import names are distinct top-level modules (`eios_core`, `eios_storage`, `eios_api`, `eios_worker`,
  `eios_mcp`) instead of a namespace package, to keep tooling simple.
