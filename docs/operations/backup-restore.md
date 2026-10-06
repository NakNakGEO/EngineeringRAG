# Backup, restore, retention, audit export

## Two kinds of backup
| | What | Use |
|---|---|---|
| **Full operational backup** | the whole Engineering OS PostgreSQL + blob volume (includes Project Vault; **stays on your machine**) | same-machine disaster recovery |
| **Portable package** | encrypted Default Vault only (no project/company data, no audit/runs/jobs, no executables) | moving to a new machine, sharing your library |

### Full backup / restore (operator commands, against Engineering OS's own database only)
```bash
scripts/backup.sh  backups/eios-$(date +%F)      # pg_dump + blob volume tarball
scripts/restore.sh backups/eios-2026-10-06       # stops api/worker/mcp, restores, restarts
```
These run `pg_dump`/`psql` *inside the Engineering OS postgres container* and are operator tools,
never reachable from the application, MCP or any model.

### Portable package (fresh-machine restore)
```bash
curl -s localhost:8000/admin/export -H "X-EIOS-Admin-Token: $EIOS_ADMIN_TOKEN" \
  -H 'content-type: application/json' -d '{"passphrase": "<>=12 chars>"}' > package.json
# on the new machine (stack up, empty database):
curl -s localhost:8000/admin/import -H "X-EIOS-Admin-Token: $EIOS_ADMIN_TOKEN" \
  -H 'content-type: application/json' \
  -d "{\"package_base64\": $(jq .package_base64 package.json), \"passphrase\": \"...\"}"
```
Add `"dry_run": true` first to see what would be imported. Imported knowledge is capped at DERIVED /
UNVERIFIED and skills/agents re-register as EXPERIMENTAL: trust is re-earned on the new machine.
`tests/integration/test_portability.py` restores a package into an empty database and scans every
table to prove no project/company data exists there.

## Retention
`POST /admin/retention` (or the `platform.retention` job) deletes: expired ephemeral knowledge,
memory and evidence (and orphaned blobs), events of runs finished more than
`EIOS_RETENTION_EVENT_DAYS` (90) ago, finished jobs older than `EIOS_RETENTION_JOB_DAYS` (14), old
decided approvals. Audit is kept forever unless `EIOS_RETENTION_AUDIT_DAYS` is set - export first.
Events and audit are append-only; only this job (it sets `eios.retention=on` in its transaction)
can delete, and UPDATE is never possible.

## Audit export
`GET /admin/audit/export` streams NDJSON where each line chains the previous hash; the last line
is `{"type":"end","count":N,"head":...}`. Verify with `eios_policy.audit.verify_ndjson`. Any edited,
removed or truncated entry breaks verification.

## Crash safety
Jobs use leases with heartbeats and fencing (a stale worker's completion is rejected); a crashed
worker's job is reclaimed after the lease expires. Index sync, workflow state and approvals use
single transactions / compare-and-set. See `tests/integration/test_job_queue.py`.
