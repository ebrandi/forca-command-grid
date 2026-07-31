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
#    1. back up the database
#    2. fast-forward the tracked branch
#    3. stamp the build revision
#    4. BUILD the new image            — old containers keep serving
#    5. AUDIT the new image (Python)   — the packages actually installed in it, not the
#                                        requirements file; refuses to go further on a CVE
#    6. AUDIT the new images (OS)      — the system packages of every image about to start,
#                                        including nginx/Postgres/Redis, which step 5
#                                        cannot see at all
#    7. MIGRATE on the new image       — one-off container, old stack still live
#    8. COLLECTSTATIC on the new image — writes the shared volume before the new web boots,
#                                        so gunicorn never starts without a static manifest
#    9. SWAP containers, then restart nginx LAST — it caches the upstream container's IP and
#       will hand out 502s until it is restarted
#   10. RE-AUDIT what is now running   — so a release that fixes a CVE clears its own
#       finding within minutes instead of leaving a stale alarm standing
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

log "1/10 Backing up the database before upgrading ..."
scripts/backup.sh ./backups || die "Backup failed — aborting upgrade."

log "2/10 Fetching and fast-forwarding '${BRANCH}' ..."
git fetch --prune origin
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH" || die "Fast-forward failed (local changes?). Resolve, then re-run."

log "3/10 Stamping the build revision ..."
[ -x deploy/stamp-version.sh ] && deploy/stamp-version.sh . || warn "stamp-version.sh missing; footer hash may hide."

log "4/10 Building the new image (the running stack keeps serving) ..."
# The image build also compiles the message catalogues, so a malformed .po fails HERE,
# before anything is swapped, rather than shipping a silently untranslated site.
$DC -f "$CF" build

log "5/10 Auditing the packages installed in the new image ..."
# Deliberately BEFORE the migration: a migration is the first irreversible step of an
# upgrade, so the "this build is not safe to run" verdict has to land while backing out
# still means doing nothing at all. audit-image.sh calls die() itself on a finding, and
# honours SKIP_DEPENDENCY_AUDIT=1 for air-gapped hosts and emergency roll-forwards.
scripts/audit-image.sh

log "6/10 Scanning the OS packages of every image this upgrade will start ..."
# Step 5 sees Python distributions and nothing else. This one reads each image's own
# system-package database — the application image's Debian layer plus nginx, Postgres and
# Redis, which contain none of our Python at all. That is not a hypothetical gap: the
# worst thing this install ever shipped was a CRITICAL OpenSSL inside a frozen
# nginx:1.27-alpine, invisible to pip-audit and to version bots alike.
# Also before the migration, for the same reason as step 5. Only FIXABLE findings fail, so
# an unactionable upstream advisory cannot wedge every deploy; SKIP_IMAGE_SCAN=1 overrides.
scripts/audit-image-os.sh

log "7/10 Applying migrations on the new image, while the old stack still serves ..."
# --no-deps: the services this needs (postgres) are already up; do not restart them.
$DC -f "$CF" run --rm --no-deps -T web python manage.py migrate --noinput \
  || die "Migration failed. The OLD stack is still serving and the database backup from step 1 is intact."

log "8/10 Collecting static files into the shared volume ..."
# Must happen BEFORE the new web container boots: WhiteNoise's manifest storage raises
# "Missing staticfiles manifest entry" and 500s if gunicorn starts without it.
$DC -f "$CF" run --rm --no-deps -T web python manage.py collectstatic --noinput \
  || die "collectstatic failed — refusing to swap containers without a static manifest."

log "9/10 Swapping containers ..."
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

log "10/10 Re-auditing what is now actually running ..."
# THE POINT OF THIS STEP, in one sentence: a release that fixes a CVE must retire its own
# alarm, immediately.
#
# The motivating incident was not a detection failure. The scheduled scan DID find 25
# Pillow advisories and DID raise a severity-80 director finding naming every CVE. The fix
# then shipped — and the finding went on claiming 25 open vulnerabilities for days,
# because nothing rescanned until the next scheduled run. That is not a cosmetic lag: an
# alarm that stays lit after the fix is precisely what teaches people that these alerts
# are noise, and the next one they ignore will be real.
#
# Both refreshes are REPORT-ONLY (--exit-zero). The deploy has already happened; failing it
# now on an advisory disclosed since the image was built would help nobody, and a deploy
# script that fails after a successful swap is one people learn to stop reading.
$DC -f "$CF" exec -T web python manage.py audit_dependencies --exit-zero --trigger deploy \
  || warn "Post-deploy dependency re-audit did not complete — the standing finding may be stale
       until the next scheduled scan. Run 'make audit-deps' once the cause is resolved."

scripts/scan-running-images.sh --trigger deploy --exit-zero \
  || warn "Post-deploy image re-scan did not complete — see above."

ok "Update complete."
