# ADR 0021: One-command local setup, a real smoke test, and loopback hardening

Status: Accepted (refines ADR 0017 portability; extends the threat model with T13).

Context. Running the system on a developer's own computer needs a path that is verified end to end,
works on Windows, macOS and Linux, and does not rely on the Python toolchain used by the project.
Everything listens on `127.0.0.1`, but a web page in the owner's browser can still reach loopback
services (DNS rebinding, cross-site form posts).

Decisions.
1. `scripts/setup_local.py` (standard library, Python 3.9+) checks the machine, creates `.env` with a
   random database password (atomically, mode 0600, never over an existing file), validates the
   workspace folder (not a home directory or drive root), starts the stack and runs the smoke test.
   `--env-only` backs `make init`. `scripts/eios_cli.py` is the cross-shell client; API text is
   stripped of control characters before printing.
2. `scripts/smoke_stack.py` is an end-to-end acceptance test of the running stack, not a unit test:
   it indexes a real sample git project, asserts a named function is ready with its source, an
   unknown name is reported missing, the Root Policy denies an external-database request, the event
   log is gap-free, approvals are off or token-gated, and an MCP client can list and call tools
   (including 401 without `EIOS_MCP_TOKEN`). It ignores system proxies for loopback. CI runs it.
3. Host validation: the API uses `TrustedHostMiddleware` (`api_allowed_hosts`, port ignored); the MCP
   server allows `localhost`, `127.0.0.1`, `[::1]` and `mcp` on any port plus matching Origins; nginx
   answers 421 to any other Host. The API and UI still have no authentication: the system is for a
   computer used by one person (documented in the threat model).
4. The content index skips more credential files and masks values of credential-named assignments
   (`CHUNK_VERSION` 2 re-chunks existing projects).

Consequences. Setup failures are reported with the likely cause. The remaining unverified surfaces
are Windows and macOS hosts and a browser session against the UI; see `docs/operations/local-setup.md`.
