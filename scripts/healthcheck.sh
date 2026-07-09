#!/usr/bin/env bash
# healthcheck.sh — report the health of a running FORCA Command Grid stack.
# Read-only; safe to run any time. Exit 0 if everything is healthy, else 1.
#
# Checks: container states, web /healthz, DB connectivity + migration state,
# Redis PING, Celery worker ping, and whether the SDE has been loaded.
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/lib.sh

DC="$(compose_cmd)"
CF="$(prod_compose_file)"
require_cmd docker
[ -f "$CF" ] || die "Compose file not found: $CF"

fail=0
check() { # <label> <cmd...>
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then ok "$label"; else warn "$label — FAILED"; fail=1; fi
}

log "Container status:"
$DC -f "$CF" ps --format 'table {{.Name}}\t{{.Status}}' || true
echo

check "web /healthz responds 200" \
  $DC -f "$CF" exec -T web python -c \
  'import urllib.request,sys; sys.exit(0 if urllib.request.urlopen("http://127.0.0.1:8000/healthz").status==200 else 1)'

check "database reachable" \
  $DC -f "$CF" exec -T web python manage.py showmigrations --list

check "no unapplied migrations" \
  sh -c "! $DC -f \"$CF\" exec -T web python manage.py showmigrations --plan 2>/dev/null | grep -q '\\[ \\]'"

check "redis PING" \
  $DC -f "$CF" exec -T redis sh -c 'redis-cli -a "$REDIS_PASSWORD" ping | grep -q PONG'

check "celery worker responds" \
  $DC -f "$CF" exec -T worker celery -A config inspect ping

# SDE loaded? (AppSetting key sde_version — the app stamps it on import.)
if $DC -f "$CF" exec -T web python manage.py shell -c \
  "from apps.admin_audit.models import AppSetting; import sys; sys.exit(0 if AppSetting.objects.filter(key='sde_version').exists() else 1)" >/dev/null 2>&1; then
  ok "SDE reference data loaded"
else
  warn "SDE not loaded — run 'make import-sde' (UI will show raw IDs until then)"
  fail=1
fi

echo
if [ "$fail" -eq 0 ]; then ok "Stack healthy."; else die "One or more health checks failed."; fi
