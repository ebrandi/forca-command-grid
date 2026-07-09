#!/usr/bin/env bash
# wait-for-services.sh — block until Postgres, Redis, and the web app are ready.
# Safe to re-run. Exits non-zero if any service is not ready within the timeout.
#
# Usage: scripts/wait-for-services.sh [timeout_seconds]   (default 180)
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/lib.sh

DC="$(compose_cmd)"
CF="$(prod_compose_file)"
TIMEOUT="${1:-180}"
deadline=$(( $(date +%s) + TIMEOUT ))

require_cmd docker
[ -f "$CF" ] || die "Compose file not found: $CF"

wait_for() {
  local name="$1"; shift
  log "Waiting for $name ..."
  until "$@" >/dev/null 2>&1; do
    [ "$(date +%s)" -lt "$deadline" ] || die "$name not ready within ${TIMEOUT}s"
    sleep 3
  done
  ok "$name ready"
}

# Postgres: pg_isready inside the container (uses env POSTGRES_USER/DB).
wait_for "Postgres" $DC -f "$CF" exec -T postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
# Redis: authenticated PING.
wait_for "Redis"    $DC -f "$CF" exec -T redis sh -c 'redis-cli -a "$REDIS_PASSWORD" ping | grep -q PONG'
# Web: the /healthz endpoint returns 200 (bypasses Host + TLS redirect).
wait_for "Web app"  $DC -f "$CF" exec -T web python -c \
  'import urllib.request,sys; sys.exit(0 if urllib.request.urlopen("http://127.0.0.1:8000/healthz").status==200 else 1)'

ok "All core services are ready."
