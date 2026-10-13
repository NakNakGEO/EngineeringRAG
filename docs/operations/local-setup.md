# Running Engineering OS on your own computer

Everything runs in Docker on your machine and listens on `127.0.0.1` only. The only database it ever
talks to is its own PostgreSQL container. Nothing is sent anywhere unless you configure an LLM
provider yourself.

## What you need

| | Windows | macOS | Linux |
|---|---|---|---|
| Docker | Docker Desktop with the **WSL2 backend** | Docker Desktop | Docker Engine + the Compose plugin |
| git | Git for Windows | `xcode-select --install` | your package manager |
| Python 3.9+ | only for the helper and smoke test (`py --version`) | same | same |
| Disk / RAM | about 8 GB free; give Docker at least 4 GB RAM | same | same |
| Free ports | 5432, 8000, 8080, 8082 (or use `--port-offset`) | same | same |

The stack itself needs no Python on your machine; the two scripts below are the only Python you run.
On Windows run the commands in PowerShell, or inside a WSL2 shell if your repositories live there.

## One command

```bash
git clone https://github.com/NakNakGEO/EngineeringRAG.git
cd EngineeringRAG
git checkout claude/exciting-tesla-16phur        # until the branch is merged
python scripts/setup_local.py --workspace-dir ~/src    # Windows: py scripts\setup_local.py --workspace-dir C:\src
```

`--workspace-dir` is the folder that **contains your repositories**. It is mounted read-only; nothing
outside it can be read, and nothing in it is ever modified (the smoke test writes one folder,
`eios-smoke-sample`, so use a folder you own). Omit it to use `./workspace` inside the checkout.

What the script does, in order: checks Docker/git/disk/ports and tells you exactly what is missing;
creates `.env` from `.env.example` with a **random** database password (an existing `.env` is never
touched); builds and starts the stack (`docker compose up -d --build --wait`; the first build takes
5-15 minutes and downloads about 2 GB); runs the smoke test; prints the URLs.

Useful options: `--check` (look at the machine, change nothing), `--no-smoke`, `--port-offset 100`
(move every port if one is taken), `--admin-token` (generate the human-approval token),
`--down` (stop the stack, keeping data).

### Without Python

```bash
cp .env.example .env            # Windows: copy .env.example .env
# edit .env: set EIOS_POSTGRES_PASSWORD (letters, digits, - _ . only) and make the password in
# EIOS_DATABASE_URL the same; set EIOS_WORKSPACE_DIR to your repositories folder
docker compose up -d --build --wait
curl http://127.0.0.1:8000/health/ready
```

## Testing it before you rely on it

```bash
python scripts/smoke_stack.py        # or: make smoke
```

It must end with `READY TO USE.` It checks, against the running stack: the API, the MCP server and
the web UI answer and the database behind them is healthy; a real git project placed in the
workspace is indexed; context for a named function comes back **ready and with its real source**;
an unknown name is reported as missing instead of guessed; search and impact analysis work; the Root
Policy **denies an external-database request**; a knowledge note round-trips and a client cannot
give itself high trust; the run's event log has no gaps; human approvals are disabled or token-gated;
and an MCP client can initialize, list the tools (no SQL, shell or database tool) and call
`get_context`. Any `FAIL` line says what to look at. To run one check: `--only "Impact"`.

The suite the project's CI runs is separate (`make check`, needs `uv`); you do not need it to use the
system.

## Use it on your own project

The helper `scripts/eios_cli.py` works the same in PowerShell, cmd, bash and zsh (no `curl` quoting).

1. Put or clone the repository inside the workspace folder, e.g. `~/src/my-service`.
2. Index it (give the folder name; it is looked up under the workspace):
   ```bash
   python scripts/eios_cli.py add my-service
   python scripts/eios_cli.py projects
   ```
3. Open the UI at http://127.0.0.1:8080: Projects shows what was understood and how much;
   Mission Control shows every run live.
4. Ask for context before changing anything:
   ```bash
   python scripts/eios_cli.py context "What does OrderService.place_order do?" -p my-service
   python scripts/eios_cli.py context "Add a retry to charge" -p my-service -s PaymentGateway --risk high
   python scripts/eios_cli.py impact -p my-service --path src/payments/gateway.py
   ```
   `NOT READY` plus the listed `missing_context` is the system telling you what it could not
   establish. Treat that as information, not as an error. `--json` prints the raw response.
5. Connect your model over MCP (`http://127.0.0.1:8082/mcp`): see
   [`docs/integrations/README.md`](../integrations/README.md). With Claude Code:
   `claude mcp add --transport http engineering-os http://127.0.0.1:8082/mcp`.

The same operations are plain HTTP (`POST /projects/bootstrap`, `POST /context/build`,
`GET /projects/{id}/impact`); the interactive reference is at http://127.0.0.1:8000/docs.

Uncommitted edits in the repository are indexed as an overlay and are the current truth for
retrieval; run `add` again after a large change.

## Defaults that matter

* **Human approvals are off** until you set `EIOS_ADMIN_TOKEN` in `.env` (or run setup with
  `--admin-token`) and restart (`docker compose up -d`). The token is for you only: never paste it
  into a chat, prompt or agent configuration.
* Set `EIOS_MCP_TOKEN` (12+ characters) if other people use the same computer.
* There is no setting for any other database, by design. For a company/QA/production database the
  system only writes SQL **as text** for a human to run.
* No LLM provider is configured by default; retrieval, indexing and policy do not need one.

## Day to day

| | |
|---|---|
| Stop / start | `python scripts/setup_local.py --down` / `docker compose up -d` |
| Logs | `docker compose logs -f api worker` |
| Update | `git pull` then `docker compose up -d --build` (migrations run automatically) |
| Back up | [`backup-restore.md`](backup-restore.md) (the shell scripts need bash: WSL on Windows) |
| Start over (**deletes all data**) | `docker compose down -v` |

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `PROBLEM: Port 5432 ... already used` | another PostgreSQL is running; re-run with `--port-offset 100` (new `.env` only) or change the ports in `.env` |
| `password authentication failed` in the `migrate`/`api` logs | `.env` was regenerated but an older database volume exists. Restore the old `.env`, or `docker compose down -v` (deletes data) |
| Bootstrap says the path is outside the approved workspace | the path must start with `/workspace/` and the folder must be inside `EIOS_WORKSPACE_DIR`; check `docker compose exec api ls /workspace` |
| The smoke test cannot write the sample | `EIOS_WORKSPACE_DIR` is read-only for you; point it at a folder you own |
| Build fails with `CERTIFICATE_VERIFY_FAILED` | a proxy re-signs HTTPS; see "Building images behind a TLS-inspecting proxy" in [`../development.md`](../development.md) |
| Build fails fetching `git` from Debian | `EIOS_RUNTIME_IMAGE=python:3.12 docker compose build` (that image already has git) |
| `docker: command not found` / daemon errors | start Docker Desktop and wait for it to say "running" |
| Windows: `exec ... no such file or directory` or odd `^M` errors | a file was checked out with CRLF line endings. `.gitattributes` prevents this on a fresh clone; if you cloned before it existed, delete the folder and clone again |
| Windows/WSL: indexing is slow | keep repositories on the WSL filesystem (`~/src`), not under `/mnt/c` |
| UI loads but shows no data | the API is not healthy: `docker compose ps`, `docker compose logs api` |

## What has and has not been verified

See the section at the end of this file, which records exactly what was run, where and when.
