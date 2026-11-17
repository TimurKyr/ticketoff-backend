#!/usr/bin/env sh
# Daily Postgres dump of the production stack; keeps 14 days of dumps.
# Cron: 15 3 * * * /opt/ticketoff/scripts/backup-db.sh >> /opt/ticketoff/backups/backup.log 2>&1
set -eu
cd "$(dirname "$0")/.."
mkdir -p backups
out="backups/ticketoff-$(date +%Y-%m-%d_%H%M).dump"
docker compose -f docker-compose.prod.yml exec -T db \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$out"
find backups -name 'ticketoff-*.dump' -mtime +14 -delete
echo "$(date -Iseconds) backup ok: $out"
