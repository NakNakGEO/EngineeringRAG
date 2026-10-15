# Development guide

## Setup

```bash
make init            # .env from .env.example (git-ignored); change the password
make sync            # uv sync: installs all workspace packages + dev tools into .venv
make db-up           # start only the Engineering OS PostgreSQL (waits until healthy)
```

Python 3.12+ is required (containers use 3.12). uv picks an installed interpreter.

## Everyday commands

| Command | What it does |
|---|---|
| `make check` | Everything CI runs: lint, mypy --strict, forbidden-deps, full tests |
| `make lint` / `make format` | ruff check and format |
| `make typecheck` | `mypy` (strict, `packages apps tests migrations scripts`) |
| `make forbid-deps` | Fail if an external-database driver / V1-excluded dependency appears |
| `make test-unit` | Unit + security tests; needs no database |
| `make test` | All tests; starts the compose PostgreSQL first |
| `make up` / `make down` / `make logs` / `make ps` | Run the full stack |

## Tests

- `tests/unit`: pure logic, ASGI apps via test clients, worker lifecycle, MCP server in memory.
- `tests/security`: Root Policy invariants (database policy, forbidden dependencies, repository
  configuration). These guard architecture rules; do not loosen them to make a change pass.
- `tests/integration` (marker `integration`): need the Engineering OS PostgreSQL. The fixture
  `test_database_url` creates a throwaway database `eios_test_<random>` on the server named by
  `EIOS_TEST_DATABASE_URL`, runs against it, and drops it. The development database is never touched.
  `make test` sets this up for you. `EIOS_REQUIRE_DB_TESTS=1` turns "no database" from a skip into a
  failure (CI and `make test` set it). Plain `pytest` without the variable skips them.

## Projects to analyse

Engineering OS only reads projects inside an approved workspace. Set `EIOS_WORKSPACE_DIR` (compose
mounts it read-only at `/workspace`) and, for host-run processes, `EIOS_WORKSPACE_ROOTS`. Then:

```bash
curl -X POST localhost:8000/projects/bootstrap -H 'content-type: application/json' \
     -d '{"path": "/workspace/my-repo"}'      # registers the project and queues an index job
```

The image installs `git` from Debian. If your network blocks that mirror, build with a base that
already includes git: `EIOS_RUNTIME_IMAGE=python:3.12 docker compose build`.

## Resetting the database / changing the password

PostgreSQL reads `EIOS_POSTGRES_PASSWORD` only when it first initialises the data volume. Editing it
in `.env` later does not change the database's password and the stack will fail to authenticate.
To start over (this DELETES all Engineering OS data): `docker compose down -v && make up`.

## Migrations (Alembic)

The URL comes from `EIOS_DATABASE_URL` through `Settings`, so the external-database policy applies.
Never put a URL in `alembic.ini`.

```bash
make migrate                         # apply all pending (host-run, needs make db-up)
make migrate-down                    # roll back ONE revision
make migrate-sql                     # print pending SQL without connecting (offline mode)
make revision m="add something"      # new revision (then edit it by hand)
docker compose run --rm migrate alembic upgrade head      # same, inside the stack
docker compose run --rm migrate alembic downgrade base    # full rollback inside the stack
```

`make up` runs `alembic upgrade head` automatically via the `migrate` service before the apps start.

Rollback notes: every revision must implement a working `downgrade()`; the integration tests run
`upgrade -> upgrade -> downgrade base -> upgrade` against a real database. Downgrades that drop data
must say so in the revision docstring. Phase 0 has a single empty baseline revision (`0001`).

## Building images behind a TLS-inspecting proxy

If `pip`/`uv` inside `docker build` fails with `CERTIFICATE_VERIFY_FAILED` because your network
re-signs HTTPS traffic, pass your **full** PEM CA bundle (system roots plus the corporate root) as a
build secret. It is mounted only during the build steps and never stored in an image layer:

```bash
EIOS_BUILD_CA_BUNDLE=/path/to/ca-bundle.pem docker compose build
```

Do not disable TLS verification.

## Import boundaries

`apps/*` may import `packages/*`; `packages/*` never import `apps/*`. `eios_core` must not depend on a
web framework or database driver. Only `eios_storage` creates database engines. Declared dependencies in
each package's `pyproject.toml` are the boundary; keep them minimal.

## Windows notes

Use Docker Desktop with the WSL2 backend and run `make` from WSL2 (or Git Bash). Paths in code must not
assume `/` or drive letters; configuration is via `EIOS_*` environment variables only.
