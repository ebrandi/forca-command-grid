#!/usr/bin/env bash
# update.sh — safely upgrade a running installation to the latest code.
#
# ORDER MATTERS, and it is not the obvious one. This script used to run
# `up -d --build` and migrate afterwards, which starts the NEW code against the OLD
# schema. Any new column on a hot table (a language preference on the user table, say)
# then makes EVERY session-bearing request fail with "column ... does not exist" for the
# whole migrate window, and Celery falls over with it. The site is down while the upgrade
# "succeeds".
#
# So: build first, then migrate and collectstatic in one-off containers on the new image
# while the OLD stack is still happily serving, and only then swap. The window in which
# the code and the schema disagree shrinks to the container swap itself.
#
#   1. back up the database
#   2. fast-forward the tracked branch
#   3. stamp the build revision
#   4. BUILD the new image            — old containers keep serving
#   5. MIGRATE on the new image       — one-off container, old stack still live
#   6. COLLECTSTATIC on the new image — writes the shared volume before the new web boots,
#                                       so gunicorn never starts without a static manifest
#   7. SWAP containers, then restart nginx LAST — it caches the upstream container's IP and
#      will hand out 502s until it is restarted
#
# Aborts on the first failure and never force-resets your checkout.
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

log "1/7 Backing up the database before upgrading ..."
scripts/backup.sh ./backups || die "Backup failed — aborting upgrade."

log "2/7 Fetching and fast-forwarding '${BRANCH}' ..."
git fetch --prune origin
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH" || die "Fast-forward failed (local changes?). Resolve, then re-run."

log "3/7 Stamping the build revision ..."
[ -x deploy/stamp-version.sh ] && deploy/stamp-version.sh . || warn "stamp-version.sh missing; footer hash may hide."

log "4/7 Building the new image (the running stack keeps serving) ..."
# The image build also compiles the message catalogues, so a malformed .po fails HERE,
# before anything is swapped, rather than shipping a silently untranslated site.
$DC -f "$CF" build

log "5/7 Applying migrations on the new image, while the old stack still serves ..."
# --no-deps: the services this needs (postgres) are already up; do not restart them.
$DC -f "$CF" run --rm --no-deps -T web python manage.py migrate --noinput \
  || die "Migration failed. The OLD stack is still serving and the database backup from step 1 is intact."

log "6/7 Collecting static files into the shared volume ..."
# Must happen BEFORE the new web container boots: WhiteNoise's manifest storage raises
# "Missing staticfiles manifest entry" and 500s if gunicorn starts without it.
$DC -f "$CF" run --rm --no-deps -T web python manage.py collectstatic --noinput \
  || die "collectstatic failed — refusing to swap containers without a static manifest."

log "7/7 Swapping containers ..."
$DC -f "$CF" up -d
scripts/wait-for-services.sh || warn "Services slow to start — continuing."

# nginx resolves the web container's IP once and caches it. A freshly swapped web container
# has a NEW IP, so nginx keeps dialling the old one and returns 502 (connection refused)
# until it is restarted. Always last, and only if this deployment actually fronts with nginx.
if $DC -f "$CF" config --services | grep -qx nginx; then
  log "Restarting nginx so it picks up the new web container's address ..."
  $DC -f "$CF" restart nginx
fi

log "Health check ..."
scripts/healthcheck.sh || warn "Health check reported issues — inspect 'make logs'."

ok "Update complete."
