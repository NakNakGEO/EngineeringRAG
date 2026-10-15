# ADR 0002: Docker-first deployment

Status: Accepted (Phase 0)

## Context
Primary development target is Windows + Docker Desktop + WSL2, with Linux supported and macOS possible
later. The system must be reproducible on a fresh machine and run locally (no cloud knowledge storage).

## Decision
`docker compose up` is the supported way to run Engineering OS. Phase 0 topology:
`postgres`, one-shot `migrate`, `api`, `worker`, `mcp`. (`web-ui` arrives in Phase 11.)

- One parameterised Dockerfile builds the three Python processes from the uv workspace
  (`--build-arg APP=eios-api|eios-worker|eios-mcp`) with a locked, non-editable, no-dev install.
- Containers run as a non-root user, with a read-only root filesystem, all capabilities dropped and
  `no-new-privileges`.
- Every published port is bound to `127.0.0.1`.
- Every service has a healthcheck against its `/health/ready` endpoint.
- Credentials come from a git-ignored `.env`; compose refuses to start without them.
- No Windows-only paths in the domain model; everything is configured through `EIOS_*` variables.
- An optional BuildKit secret (`extra_ca`) lets networks that inspect TLS supply a CA bundle for image
  builds without changing the Dockerfile or baking the CA into a layer.

## Consequences
- Host-run development (`uv run ...`) is still supported against the compose PostgreSQL.
- Database migrations run as a dedicated compose step before any app container starts.
- Base images are pinned by tag (not digest) for now; digest pinning is a Phase 12 hardening item.
