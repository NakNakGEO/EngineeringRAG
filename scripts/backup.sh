#!/usr/bin/env bash
# Operator backup of Engineering OS's OWN database and blob volume (never an external database).
set -euo pipefail
out="${1:?usage: scripts/backup.sh <output-prefix>}"
set -a; [ -f .env ] && . ./.env; set +a
mkdir -p "$(dirname "$out")"
docker compose exec -T postgres pg_dump -U "${EIOS_POSTGRES_USER}" -d "${EIOS_POSTGRES_DB}" -Fc > "${out}.dump"
docker run --rm -v engineering-intelligence-os_eios-blobs:/data:ro -v "$(cd "$(dirname "$out")" && pwd)":/out \
  alpine tar -C /data -czf "/out/$(basename "$out").blobs.tgz" .
sha256sum "${out}.dump" "${out}.blobs.tgz" > "${out}.sha256"
echo "backup written: ${out}.dump ${out}.blobs.tgz (checksums in ${out}.sha256)"
