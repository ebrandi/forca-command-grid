#!/usr/bin/env bash
# update.sh — safely upgrade a running installation to the latest code.
#
# Steps: back up the DB, pull the tracked branch (fast-forward only), stamp the
# build, rebuild images, apply migrations, collectstatic, and restart. Aborts on
# the first failure and never force-resets your checkout.
#
# Usage: scripts/update.sh [branch]     (default: current branch)
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/lib.sh

DC="$(compose_cmd)"
CF="$(prod_compose_file)"
require_cmd docker
require_cmd git
[ -f "$CF" ] || die "Compose file not found: $CF"

BRANCH="${1:-$(git rev-parse --abbrev-ref HEAD)}"

log "1/6 Backing up the database before upgrading ..."
scripts/backup.sh ./backups || die "Backup failed — aborting upgrade."

log "2/6 Fetching and fast-forwarding '${BRANCH}' ..."
git fetch --prune origin
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH" || die "Fast-forward failed (local changes?). Resolve, then re-run."

log "3/6 Stamping the build revision ..."
[ -x deploy/stamp-version.sh ] && deploy/stamp-version.sh . || warn "stamp-version.sh missing; footer hash may hide."

log "4/6 Rebuilding and restarting the stack ..."
$DC -f "$CF" up -d --build

log "5/6 Waiting for services, then migrating ..."
scripts/wait-for-services.sh
$DC -f "$CF" exec -T web python manage.py migrate --noinput
$DC -f "$CF" exec -T web python manage.py collectstatic --noinput

log "6/6 Health check ..."
scripts/healthcheck.sh || warn "Health check reported issues — inspect 'make logs'."

ok "Update complete."
