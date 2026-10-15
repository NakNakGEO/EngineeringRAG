#!/usr/bin/env bash
# Operator restore of a backup made by scripts/backup.sh into Engineering OS's own database.
set -euo pipefail
in="${1:?usage: scripts/restore.sh <backup-prefix>}"
set -a; [ -f .env ] && . ./.env; set +a
sha256sum -c "${in}.sha256"
docker compose stop api worker mcp web-ui >/dev/null 2>&1 || true
docker compose up -d --wait postgres
docker compose exec -T postgres psql -U "${EIOS_POSTGRES_USER}" -d postgres -c "DROP DATABASE IF EXISTS ${EIOS_POSTGRES_DB} WITH (FORCE)" -c "CREATE DATABASE ${EIOS_POSTGRES_DB}"
docker compose exec -T postgres pg_restore -U "${EIOS_POSTGRES_USER}" -d "${EIOS_POSTGRES_DB}" --no-owner < "${in}.dump"
docker run --rm -v engineering-intelligence-os_eios-blobs:/data -v "$(cd "$(dirname "$in")" && pwd)":/in:ro \
  alpine sh -c "rm -rf /data/* && tar -C /data -xzf /in/$(basename "$in").blobs.tgz"
docker compose up -d --wait
echo "restore complete"
