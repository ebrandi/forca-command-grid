#!/usr/bin/env bash
# backup.sh — dump the PostgreSQL database to a timestamped gzip file.
# Safe to run any time (read-only against the DB). Does NOT print secrets.
#
# Usage: scripts/backup.sh [output_dir]     (default: ./backups)
# Retention: keeps the most recent BACKUP_KEEP dumps (default 14).
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/lib.sh

DC="$(compose_cmd)"
CF="$(prod_compose_file)"
OUT_DIR="${1:-./backups}"
KEEP="${BACKUP_KEEP:-14}"

require_cmd docker
[ -f "$CF" ] || die "Compose file not found: $CF"
mkdir -p "$OUT_DIR"

ts="$(date +%Y%m%d-%H%M%S)"
tmp="$(mktemp "${OUT_DIR}/.forca-${ts}.XXXXXX.sql.gz")"
final="${OUT_DIR}/forca-${ts}.sql.gz"
# Clean up the temp file if the dump fails partway.
trap 'rm -f "$tmp"' EXIT

log "Dumping database to ${final} ..."
# pg_dump inside the container; POSTGRES_USER/DB come from the container env.
# Write to a temp file first, then atomically move — a crashed dump never leaves
# a truncated file that looks valid.
$DC -f "$CF" exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > "$tmp"

# Sanity: a valid gzip with non-trivial size (a failed dump is often ~20 bytes).
gzip -t "$tmp" || die "Backup is not a valid gzip — aborting."
size=$(wc -c < "$tmp")
[ "$size" -gt 1000 ] || die "Backup suspiciously small (${size} bytes) — aborting."

mv "$tmp" "$final"
trap - EXIT
ok "Backup written: ${final} (${size} bytes)"

# Prune old dumps beyond retention (never touches anything else).
if [ "$KEEP" -gt 0 ]; then
  # shellcheck disable=SC2012
  ls -1t "${OUT_DIR}"/forca-*.sql.gz 2>/dev/null | tail -n "+$((KEEP + 1))" | while read -r old; do
    log "Pruning old backup: $old"; rm -f "$old"
  done
fi
ok "Done. Retained the newest ${KEEP} dumps in ${OUT_DIR}."
