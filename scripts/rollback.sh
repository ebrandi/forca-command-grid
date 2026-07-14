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
#
#   "The migrations were additive, so the old code will tolerate them" IS NOT TRUE, and
#   this script used to say that it was. Believing it has already broken this installation.
#   `AddField(default=...)` enforces its default in Python, not in the database: Django adds
#   the column NOT NULL and then immediately DROPs the default. Roll the code back and reads
#   keep working, /healthz keeps passing, the site looks fine — while every INSERT from the
#   older code (which does not know the column exists, and so never supplies it) dies on a
#   not-null violation. Silent, delayed, and it lands on whatever writes first: new-pilot
#   registration, alert emission, a Celery task at 3am.
#
#   So we no longer ask you to judge this. Before a code-only rollback the script runs
#   `manage.py rollback_safety`, which asks the DATABASE what the columns left behind
#   actually look like and refuses if any of them is NOT NULL with no database default (or
#   if a migration did something outright destructive). Pass --accept-schema-drift to
#   override once you have read the list and understood which tables become unwritable.
#
#   Rolling back CODE ONLY (no --restore) is fast and keeps data written since the upgrade.
#   Rolling back CODE + DATABASE (--restore) is the only fully consistent option, and it
#   DISCARDS every row written since that dump was taken.
#
# Either way this script takes a fresh safety backup first, so the rollback itself is
# reversible.
#
# Usage:
#   scripts/rollback.sh <git-ref> [--restore <dump.sql.gz>] [--yes] [--accept-schema-drift]
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
ACCEPT_DRIFT=0
shift || true
while [ $# -gt 0 ]; do
  case "$1" in
    --restore)              DUMP="${2:-}"; shift 2 ;;
    --yes)                  ASSUME_YES=1; shift ;;
    --accept-schema-drift)  ACCEPT_DRIFT=1; shift ;;
    *)                      die "Unknown argument: $1 (usage: scripts/rollback.sh <git-ref> [--restore <dump.sql.gz>] [--yes] [--accept-schema-drift])" ;;
  esac
done

[ -n "$REF" ] || die "Usage: scripts/rollback.sh <git-ref> [--restore <dump.sql.gz>] [--yes] [--accept-schema-drift]"
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

# ---------------------------------------------------------------------------------------
# Schema-drift gate. Only a CODE-only rollback can be poisoned by the schema it leaves
# behind; --restore rewinds the database too, so there is nothing to check.
#
# This runs while the CURRENT stack is still up, because the current image is the only one
# that has the migration files we need to inspect. See the header, and
# apps/admin_audit/management/commands/rollback_safety.py.
# ---------------------------------------------------------------------------------------
if [ -z "$DUMP" ]; then
  log "Checking whether the schema left behind is safe for the older code ..."

  # Migrations present in the CURRENT tree but absent from the rollback target — i.e. the
  # ones that will still be applied in the database after the code goes back.
  SAFETY_ARGS=""
  while IFS= read -r m; do
    [ -n "$m" ] || continue
    case "$m" in */__init__.py) continue ;; esac
    app="$(printf '%s' "$m" | cut -d/ -f2)"
    name="$(basename "$m" .py)"
    SAFETY_ARGS="$SAFETY_ARGS --migration ${app}.${name}"
  done <<EOF
$(git diff --diff-filter=A --name-only "$TARGET" "$CURRENT" -- 'apps/*/migrations/*.py')
EOF

  if [ -z "$SAFETY_ARGS" ]; then
    ok "No migrations are being left behind — nothing can drift."
  else
    # The check needs the CURRENT stack up: it reads the live schema, and only the current
    # image carries the migration files. If the stack is down we cannot verify — say that
    # plainly rather than reporting a schema problem we never actually looked for.
    # `ps -q web` is empty when the service is not running, and is understood by both the
    # compose plugin and the legacy docker-compose that lib.sh still falls back to.
    if [ -z "$($DC -f "$CF" ps -q web 2>/dev/null)" ]; then
      [ "$ACCEPT_DRIFT" -eq 1 ] \
        || die "$(printf '%s\n' \
             "Cannot check schema safety: the 'web' container is not running, so there is" \
             "nothing to inspect the database with." \
             "" \
             "  Start the stack ('make up') and re-run, or" \
             "  re-run with --restore <dump.sql.gz>, which rewinds the schema and needs no check, or" \
             "  re-run with --accept-schema-drift to skip the check entirely.")"
      warn "Stack is down, so the schema check was skipped (--accept-schema-drift)."
      SAFETY_ARGS=""
    fi
  fi

  if [ -n "$SAFETY_ARGS" ]; then
    # shellcheck disable=SC2086  # word splitting is what builds the repeated --migration flags
    if $DC -f "$CF" exec -T web python manage.py rollback_safety $SAFETY_ARGS; then
      ok "Schema check passed — the older code can still write to every affected table."
    else
      echo
      if [ "$ACCEPT_DRIFT" -eq 1 ]; then
        warn "Schema check FAILED, but --accept-schema-drift was given."
        warn "The tables listed above will be READ-ONLY to the rolled-back code: any INSERT"
        warn "into them will raise a not-null violation. Proceeding because you asked."
      else
        die "$(printf '%s\n' \
          "A code-only rollback is NOT safe against this schema (details above)." \
          "" \
          "  Safest:   re-run with --restore <dump.sql.gz> to rewind the database too." \
          "  Or:       give the listed columns a database default (the ALTER TABLE above)." \
          "  Or:       reverse the migrations first, using the CURRENT image." \
          "  Override: re-run with --accept-schema-drift if you have read the list and" \
          "            accept that those tables become unwritable.")"
      fi
    fi
  fi
fi

echo
warn "About to roll back:"
warn "  from  ${CURRENT:0:12}  $(git log -1 --format=%s "$CURRENT" | cut -c1-60)"
warn "  to    ${TARGET:0:12}  $(git log -1 --format=%s "$TARGET" | cut -c1-60)"
if [ -n "$DUMP" ]; then
  warn "  database WILL be restored from ${DUMP}"
  warn "  every row written since that dump will be LOST"
else
  warn "  database will be left ALONE (schema stays at its current, newer migration state)"
  if [ "$ACCEPT_DRIFT" -eq 1 ]; then
    warn "  --accept-schema-drift: the schema check was overridden, some tables may be unwritable"
  else
    warn "  the schema check above confirmed the older code can still write to every table"
  fi
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
