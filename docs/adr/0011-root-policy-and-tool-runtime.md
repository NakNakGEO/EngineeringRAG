# ADR 0011: Root Policy, Policy Engine, approvals and the tool runtime

Status: Accepted (Phase 6).

## Root Policy
- `policy/root_policy.yaml` + `policy/root_policy.lock` (SHA-256 of the canonical document). The
  digest is verified on every start; any mismatch, a missing lock, a symlink or invalid content
  aborts startup (**fail closed**).
- The loaded `RootPolicy` is a frozen object with immutable collections. No API writes it
  (`GET /policy/root` is read-only; `POST /policy/evaluate` is a dry run). A security test walks
  the route table to prove this.
- Structural minimums are compiled in (external-database capabilities, database client binaries,
  database ports, git actions needing approval) and merged into whatever the file says, so a
  weakened-and-relocked file cannot remove them.

## Policy Engine (default deny, first match wins)
1. database actions / external URLs / database clients / database ports: always DENY
   (`root.external_database`). Even the internal database is closed to LLMs, agents and tools.
2. forbidden capabilities: DENY. 3. approval-gated git actions (push, merge, rebase, reset --hard,
   release, force push, history rewrite): REQUIRE_APPROVAL. 4. filesystem scopes (realpath-resolved,
   symlink/traversal safe, secret-looking paths and `.git` internals denied, writes need an explicit
   writer), network allowlist (private/metadata addresses and internal hosts denied), process
   allowlist (registered + hash-pinned only), secret grants, capability invocation (state, origin,
   approval, risk vs. maturity). 5. anything else: DENY.
- Every decision is written to the append-only `policy.audit_log` (DB trigger) and emitted as
  `POLICY_ALLOWED/POLICY_DENIED` events.

## Approvals
- The engine can *request* an approval but has no way to grant one. A decision needs a named human
  and `X-EIOS-Admin-Token` (`EIOS_ADMIN_TOKEN`, >= 12 chars; blank = approvals disabled).
- Approvals are bound to a fingerprint (action + capability + target + actor), expire, and are
  **single use** (consumed atomically with `SKIP LOCKED`).

## Tool runtime and sandbox
- `ToolRuntime.invoke`: route -> policy -> (approval) -> execute -> metrics/events. The only way a
  capability executes. Failures are reported, never raised into the LLM loop.
- `SubprocessSandbox`: absolute path, SHA-256 pinned (re-verified at run time), not world-writable,
  no shell, scrubbed environment, rlimits (CPU, memory, file size, open files; NPROC is opt-in), timeout with process-group kill, output cap, network
  namespace isolation via `unshare -rn`. If isolation is required and unavailable it **refuses to
  run**. Stronger sandboxes plug in behind the `Sandbox` protocol.
- `SecretsBroker`: secrets from `EIOS_SECRET_*`, granted per manifest; anything that looks like
  database connection material (by name or value) is refused outright.

## Known limits
- The subprocess sandbox is not a VM; hostile native code needs a container/microVM sandbox
  (future implementation of `Sandbox`). In the default compose images user namespaces may be
  unavailable, in which case network-isolated subprocess tools are refused rather than run.
