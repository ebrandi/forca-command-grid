#!/usr/bin/env bash
# restore.sh — restore the PostgreSQL database from a gzip dump made by backup.sh.
#
# DESTRUCTIVE: this DROPs and recreates the application schema. It refuses to run
# without an explicit confirmation. Take a fresh backup first.
#
# Usage: scripts/restore.sh <dump.sql.gz> [--yes]
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/lib.sh

DC="$(compose_cmd)"
CF="$(prod_compose_file)"
DUMP="${1:-}"
CONFIRM="${2:-}"

require_cmd docker
[ -f "$CF" ] || die "Compose file not found: $CF"
[ -n "$DUMP" ] || die "Usage: scripts/restore.sh <dump.sql.gz> [--yes]"
[ -f "$DUMP" ] || die "Dump file not found: $DUMP"
gzip -t "$DUMP" || die "Not a valid gzip file: $DUMP"

if [ "$CONFIRM" != "--yes" ]; then
  warn "This will OVERWRITE the current database with: $DUMP"
  warn "All data since that dump will be lost. Re-run with --yes to proceed."
  printf 'Type the word RESTORE to continue: '
  read -r answer
  [ "$answer" = "RESTORE" ] || die "Aborted."
fi

log "Taking a safety backup before restore ..."
scripts/backup.sh ./backups >/dev/null || warn "Pre-restore backup failed; continuing on your confirmation."

log "Restoring database from ${DUMP} ..."
# Recreate a clean public schema, then pipe the dump in. Run inside the postgres
# container so no host psql is required; credentials come from container env.
gunzip -c "$DUMP" | $DC -f "$CF" exec -T postgres sh -c '
  set -e
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
    -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1
'

log "Applying any migrations newer than the dump ..."
$DC -f "$CF" exec -T web python manage.py migrate --noinput

ok "Restore complete. Run 'make health' to verify."
