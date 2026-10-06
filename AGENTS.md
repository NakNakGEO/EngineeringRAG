# Instructions for coding agents (Codex, Claude, Hermes, others)

Read `docs/architecture/MASTER_PLAN_AND_PROMPTS.md` before changing code. Implement one phase at a
time, only when the human owner asks for it.

## Non-negotiable
1. Python-first, Docker-first, local-first. Modular monolith. No microservices.
2. PostgreSQL belongs exclusively to Engineering OS. **Never** add a connector, driver, URL, credential
   or code path for any external/company/QA/production database. External DB work = generate SQL as
   text for a human to run. `make forbid-deps` and `tests/security` enforce this: never weaken them to
   make something pass.
3. No Kubernetes, Kafka, Neo4j, Qdrant, Redis or cloud storage in V1 without an explicit owner decision.
4. Root Policy cannot be changed by agents, tools, skills, plugins or generated code.
5. Domain code must not import FastAPI, vendor LLM SDKs, the Docker SDK or vendor tool SDKs.
6. Every important action must emit an observability event (from Phase 1).
7. Default execution model: one writer, many reviewers. Never run git push/merge/rebase/release
   without explicit human approval.

## Working agreement
- Before coding: inspect the repo, map it to the target architecture, list conflicts.
- Keep `docker compose up` working. Add migrations for schema changes, plus unit, integration and
  (where permissions are involved) security tests.
- Run `make check` (ruff, mypy --strict, forbidden-deps, pytest) and fix failures instead of
  disabling checks.
- Record decisions in `docs/adr/`; document deviations from the master plan.
- No secrets in the repo, no company data in fixtures.
