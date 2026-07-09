#!/usr/bin/env bash
# create-admin.sh — ensure a Django superuser exists (idempotent).
#
# In normal operation FORCA users log in with EVE SSO and roles are granted in
# the app; a Django superuser is only a break-glass account for the stock
# /admin (which is disabled unless DJANGO_ENABLE_ADMIN=1). Creating one is still
# useful for first-run setup and emergencies.
#
# The password is read from the DJANGO_SUPERUSER_PASSWORD env var (never passed
# on the command line, never printed). If unset, you'll be prompted interactively.
#
# Usage:
#   DJANGO_SUPERUSER_PASSWORD=... scripts/create-admin.sh <email> [username]
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/lib.sh

DC="$(compose_cmd)"
CF="$(prod_compose_file)"
EMAIL="${1:-}"
USERNAME="${2:-admin}"

require_cmd docker
[ -f "$CF" ] || die "Compose file not found: $CF"
[ -n "$EMAIL" ] || die "Usage: scripts/create-admin.sh <email> [username]"

if [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]; then
  log "Ensuring superuser '${USERNAME}' <${EMAIL}> (non-interactive) ..."
  # --noinput reads DJANGO_SUPERUSER_PASSWORD from the environment; we pass it
  # through to the container without echoing it anywhere.
  $DC -f "$CF" exec -T \
    -e DJANGO_SUPERUSER_PASSWORD \
    -e DJANGO_SUPERUSER_EMAIL="$EMAIL" \
    -e DJANGO_SUPERUSER_USERNAME="$USERNAME" \
    web python manage.py createsuperuser --noinput 2>/dev/null \
    && ok "Superuser ensured." \
    || warn "Superuser already exists (or creation was rejected) — no change."
else
  warn "DJANGO_SUPERUSER_PASSWORD not set — starting interactive creation."
  $DC -f "$CF" exec web python manage.py createsuperuser --email "$EMAIL" --username "$USERNAME"
fi
