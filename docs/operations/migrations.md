# Migrations and rollback

Alembic, linear history (`migrations/versions/0001`..), applied by the one-shot `migrate` service.

- Upgrade: `make migrate` (or `docker compose run --rm migrate`). Idempotent.
- Rollback one step: `uv run alembic downgrade -1`; to a revision: `... downgrade 0005`.
  **Every downgrade is destructive for the tables it drops** - the docstring of each migration says
  exactly what is lost. Take `scripts/backup.sh` first.
- Tests: `tests/integration/test_migrations.py` runs upgrade -> downgrade base -> upgrade on a
  throwaway database, and `test_schema_drift.py` fails if the models differ from the migrations.
- Changing a schema: edit `eios_storage/tables/*`, generate with
  `alembic revision --autogenerate`, add schema creation/triggers by hand, document data loss in the
  docstring, run both tests.

| Revision | Adds |
|---|---|
| 0001 | baseline |
| 0002 | runs + append-only event log |
| 0003 | knowledge, evidence, memory, decisions |
| 0004 | projects, source, graph, jobs |
| 0005 | capability/agent/skill registries |
| 0006 | policy approvals + append-only audit log |
| 0007 | workflow runs |
| 0008 | knowledge governance + evaluation runs |
| 0009 | workshop proposals |
