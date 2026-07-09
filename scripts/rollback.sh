#!/usr/bin/env bash
# rollback.sh — return a running installation to an earlier commit, and optionally
# to the database dump that was taken alongside it.
#
# scripts/update.sh always dumps the database before it migrates, so the ingredients
# for a rollback exist for every upgrade. This script composes them.
#
# IMPORTANT — read before choosing whether to pass --restore:
#
#   Django never un-applies migrations here. If the upgrade you are undoing added
#   migrations, the database schema is NEWER than the code you are rolling back to.
#   Additive migrations (a new nullable column, a new table) are usually tolerated by
#   the older code; a destructive or altering migration is not, and the old code will
#   break in ways that are hard to diagnose.
#
#   Rolling back CODE ONLY (no --restore) is fast and keeps data written since the
#   upgrade. Use it when the upgrade added no migrations, or only additive ones.
#
#   Rolling back CODE + DATABASE (--restore) is the only fully consistent option.
#   It DISCARDS every row written since that dump was taken.
#
# Either way this script takes a fresh safety backup first, so the rollback itself is
# reversible.
#
# Usage:
#   scripts/rollback.sh <git-ref> [--restore <dump.sql.gz>] [--yes]
#
#   scripts/rollback.sh v1.0.0
#   scripts/rollback.sh HEAD~1 --restore ./backups/forca-20260709-031500.sql.gz --yes
#
# List what you can roll back to:
#   git log --oneline -10          # commits
#   ls -1t ./backups/*.sql.gz      # dumps, newest first
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/lib.sh

DC="$(compose_cmd)"
CF="$(prod_compose_file)"
require_cmd docker
require_cmd git
[ -f "$CF" ] || die "Compose file not found: $CF"

REF="${1:-}"
DUMP=""
ASSUME_YES=0
shift || true
while [ $# -gt 0 ]; do
  case "$1" in
    --restore) DUMP="${2:-}"; shift 2 ;;
    --yes)     ASSUME_YES=1; shift ;;
    *)         die "Unknown argument: $1 (usage: scripts/rollback.sh <git-ref> [--restore <dump.sql.gz>] [--yes])" ;;
  esac
done

[ -n "$REF" ] || die "Usage: scripts/rollback.sh <git-ref> [--restore <dump.sql.gz>] [--yes]"
git rev-parse --verify --quiet "${REF}^{commit}" >/dev/null \
  || die "Not a commit this repository knows about: ${REF} (try 'git fetch --all' first)."
if [ -n "$DUMP" ]; then
  [ -f "$DUMP" ] || die "Dump file not found: $DUMP"
  gzip -t "$DUMP" || die "Not a valid gzip file: $DUMP"
fi

CURRENT="$(git rev-parse HEAD)"
TARGET="$(git rev-parse "${REF}^{commit}")"
[ "$CURRENT" != "$TARGET" ] || die "Already at ${REF} (${TARGET:0:12}) — nothing to roll back."

# Refuse to throw away uncommitted work.
git diff --quiet && git diff --cached --quiet \
  || die "You have uncommitted changes. Commit or stash them before rolling back."

echo
warn "About to roll back:"
warn "  from  ${CURRENT:0:12}  $(git log -1 --format=%s "$CURRENT" | cut -c1-60)"
warn "  to    ${TARGET:0:12}  $(git log -1 --format=%s "$TARGET" | cut -c1-60)"
if [ -n "$DUMP" ]; then
  warn "  database WILL be restored from ${DUMP}"
  warn "  every row written since that dump will be LOST"
else
  warn "  database will be left ALONE (schema stays at its current, newer migration state)"
  warn "  if the upgrade you are undoing altered or dropped columns, the older code may fail"
fi
echo
if [ "$ASSUME_YES" -ne 1 ]; then
  printf 'Type the word ROLLBACK to continue: '
  read -r answer
  [ "$answer" = "ROLLBACK" ] || die "Aborted."
fi

# Remember where we came from, so going forward again is a copy-paste.
log "0/5 Recording the current revision for a return trip ..."
printf '%s\n' "$CURRENT" > .rollback-from
ok "Saved ${CURRENT:0:12} to .rollback-from — 'scripts/rollback.sh \$(cat .rollback-from)' returns here."

log "1/5 Taking a safety backup of the CURRENT database ..."
scripts/backup.sh ./backups || die "Safety backup failed — refusing to roll back blind."

log "2/5 Checking out ${REF} (${TARGET:0:12}) ..."
# Detached HEAD is correct: a rollback is a deliberate pin to a known-good revision,
# not a branch move. `git checkout <branch>` afterwards returns to normal.
git checkout --quiet --detach "$TARGET"

log "3/5 Stamping the build revision and rebuilding ..."
[ -x deploy/stamp-version.sh ] && deploy/stamp-version.sh . || warn "stamp-version.sh missing; footer hash may hide."
$DC -f "$CF" up -d --build

log "4/5 Waiting for services ..."
scripts/wait-for-services.sh || warn "Services slow to start — continuing."

if [ -n "$DUMP" ]; then
  log "5/5 Restoring the database from ${DUMP} ..."
  # restore.sh takes its own pre-restore backup and drops/recreates the schema.
  scripts/restore.sh "$DUMP" --yes || die "Restore failed. The stack is on the old code with the NEW database."
else
  log "5/5 Skipping database restore (code-only rollback) ..."
  # collectstatic only — deliberately NOT running `migrate`, which on older code would
  # be a no-op at best and could not undo the newer schema anyway.
  $DC -f "$CF" exec -T web python manage.py collectstatic --noinput
fi

log "Health check ..."
scripts/healthcheck.sh || warn "Health check reported issues — inspect 'make logs'."

echo
ok "Rolled back to ${TARGET:0:12}."
echo "  You are on a detached HEAD. To return to the branch tip:  git checkout main"
echo "  To return to where you were:                              scripts/rollback.sh \$(cat .rollback-from)"
