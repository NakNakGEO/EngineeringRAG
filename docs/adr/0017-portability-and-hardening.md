# ADR 0017: Portability, retention, audit export and hardening

Status: Accepted (Phase 12).

- **Portable package**: `.eiospkg` = magic + authenticated header + AES-256-GCM(gzip(JSON)) with a
  scrypt-derived key (>= 12 char passphrase; hostile KDF parameters refused; decompression bounded).
  Content is the Default Vault plus non-builtin skills/agents; tool entries are informational
  (executables never travel). Import re-checks every row (anything outside the Default Vault or
  referencing a project/run rejects the whole package), is all-or-nothing and idempotent, recomputes
  embeddings, and caps imported trust at DERIVED/UNVERIFIED.
- **Fresh-machine restore** is a test (`test_portability`): export from a populated database,
  import into an empty one with an empty blob store, then scan every table for company data.
- **Backup/restore** of the operational database is an operator tool (`scripts/backup.sh`,
  `restore.sh`) that runs inside the Engineering OS postgres container only.
- **Retention** is the single, explicit delete path for append-only tables; audit retention is
  opt-in. **Audit export** is hash-chained NDJSON with an end marker.
- **Hardening tests**: security (adversarial), performance budgets (generous, order-of-magnitude),
  recovery (job leases/fencing, atomic imports and retention).
- Admin endpoints require the admin token and are not reachable from MCP.
