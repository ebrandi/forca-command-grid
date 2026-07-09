#!/usr/bin/env bash
#
# verify-prod.sh — confirm the production capacity tuning is actually LIVE,
# not just present in config files. Checks the running process tree, the
# database's own active settings, and connection-pool behaviour.
#
# Usage:
#   # On the prod host, from the app dir:
#   ./deploy/verify-prod.sh
#
#   # From your workstation (re-execs itself over SSH on the host):
#   ./deploy/verify-prod.sh --remote
#
# Remote knobs (env): FORCA_SSH_KEY, FORCA_HOST, FORCA_APP_DIR.
# Exits non-zero if any hard check fails.

set -uo pipefail

# --- remote re-exec: pipe this script to the host and run it there ----------
if [[ "${1:-}" == "--remote" ]]; then
  # Require the target from the environment — never commit the production host IP,
  # login user, or key path into the repo (it is high-value recon for an attacker and
  # this file is pushed to GitHub). Set them in your shell, e.g.:
  #   FORCA_SSH_KEY=~/.ssh/your_key FORCA_HOST=deployer@your-host ./deploy/verify-prod.sh --remote
  KEY="${FORCA_SSH_KEY:?set FORCA_SSH_KEY to your prod SSH key path}"
  HOST="${FORCA_HOST:?set FORCA_HOST, e.g. deployer@host.example.com}"
  DIR="${FORCA_APP_DIR:-/opt/forca/app}"
  exec ssh -i "$KEY" -o ConnectTimeout=20 "$HOST" "cd '$DIR' && bash -s" < "$0"
fi

COMPOSE="${COMPOSE:-docker compose -f docker-compose.prod.yml}"

# Expected values (keep in sync with docker-compose.prod.yml / settings/prod.py).
EXP_GUNICORN_WORKERS=5
EXP_GUNICORN_THREADS=4
EXP_CELERY_CONCURRENCY=8
EXP_SHARED_BUFFERS="2GB"
EXP_EFFECTIVE_CACHE="12GB"
EXP_WORK_MEM="16MB"
EXP_MAX_CONNECTIONS="150"
EXP_PARALLEL_PER_GATHER="4"
EXP_CONN_MAX_AGE="60"

FAILED=0
green=$'\033[32m'; red=$'\033[31m'; dim=$'\033[2m'; rst=$'\033[0m'
pass() { printf "  ${green}PASS${rst} %s\n" "$1"; }
fail() { printf "  ${red}FAIL${rst} %s\n" "$1"; FAILED=1; }
info() { printf "  ${dim}%s${rst}\n" "$1"; }
hdr()  { printf "\n== %s ==\n" "$1"; }

psql_t() { # run a query, return tab/newline-trimmed scalar(s)
  # </dev/null: keep `compose exec` from consuming this script's stdin when it is
  # piped in (e.g. via `bash -s` in --remote mode).
  $COMPOSE exec -T postgres sh -c \
    "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -tAc \"$1\"" </dev/null 2>/dev/null | tr -d '\r'
}

# --- 1. Gunicorn: workers + threads actually spawned ------------------------
hdr "Gunicorn (web)"
web_cid="$($COMPOSE ps -q web 2>/dev/null)"
if [[ -z "$web_cid" ]]; then
  fail "web container not running"
else
  gline="$(docker top "$web_cid" -eo pid,ppid,nlwp,args 2>/dev/null | grep 'config.wsgi' || true)"
  gtotal="$(printf '%s\n' "$gline" | grep -c 'config.wsgi')"
  gworkers=$(( gtotal > 0 ? gtotal - 1 : 0 ))   # one master + N workers
  if [[ "$gworkers" -eq "$EXP_GUNICORN_WORKERS" ]]; then
    pass "$gworkers worker processes running (master + $gworkers)"
  else
    fail "expected $EXP_GUNICORN_WORKERS workers, found $gworkers"
  fi
  if printf '%s' "$gline" | grep -q -- "--worker-class gthread"; then
    pass "worker-class = gthread"
  else
    fail "worker-class is not gthread"
  fi
  if printf '%s' "$gline" | grep -q -- "--threads $EXP_GUNICORN_THREADS"; then
    pass "threads = $EXP_GUNICORN_THREADS (≈ $(( EXP_GUNICORN_WORKERS * EXP_GUNICORN_THREADS )) concurrent requests)"
  else
    fail "threads != $EXP_GUNICORN_THREADS"
  fi
fi

# --- 2. Celery: prefork children -------------------------------------------
hdr "Celery (worker)"
worker_cid="$($COMPOSE ps -q worker 2>/dev/null)"
if [[ -z "$worker_cid" ]]; then
  fail "worker container not running"
else
  ctotal="$(docker top "$worker_cid" -eo pid,args 2>/dev/null | grep -c 'celery')"
  cchildren=$(( ctotal > 0 ? ctotal - 1 : 0 ))   # one master + N children
  if [[ "$cchildren" -ge "$EXP_CELERY_CONCURRENCY" ]]; then
    pass "$cchildren prefork children (concurrency $EXP_CELERY_CONCURRENCY)"
  else
    fail "expected $EXP_CELERY_CONCURRENCY children, found $cchildren"
  fi
fi

# --- 3. Postgres: OUR -c flags are the ACTIVE source -----------------------
hdr "Postgres (active settings)"
check_pg() { # name  expected  show-value
  local got src
  got="$(psql_t "show $1")"
  src="$(psql_t "select source from pg_settings where name='$1'")"
  if [[ "$got" == "$2" && "$src" == "command line" ]]; then
    pass "$1 = $got (source: command line)"
  else
    fail "$1 = '${got:-?}' (source: '${src:-?}', expected $2 / command line)"
  fi
}
if [[ -z "$($COMPOSE ps -q postgres 2>/dev/null)" ]]; then
  fail "postgres container not running"
else
  check_pg shared_buffers "$EXP_SHARED_BUFFERS"
  check_pg effective_cache_size "$EXP_EFFECTIVE_CACHE"
  check_pg work_mem "$EXP_WORK_MEM"
  check_pg max_connections "$EXP_MAX_CONNECTIONS"
  check_pg max_parallel_workers_per_gather "$EXP_PARALLEL_PER_GATHER"
  shm="$($COMPOSE exec -T postgres sh -c 'df -m /dev/shm | awk "NR==2{print \$2}"' </dev/null 2>/dev/null | tr -d '\r')"
  if [[ -n "$shm" && "$shm" -ge 512 ]]; then
    pass "/dev/shm = ${shm} MB (parallel-query headroom)"
  else
    fail "/dev/shm = ${shm:-?} MB (expected ≥ 512, Docker default is 64)"
  fi
fi

# --- 4. Django: CONN_MAX_AGE loaded ----------------------------------------
hdr "Django (DB connection reuse)"
dj="$($COMPOSE exec -T web python -c \
  "from django.conf import settings as s; d=s.DATABASES['default']; print(d.get('CONN_MAX_AGE'), d.get('CONN_HEALTH_CHECKS'))" \
  </dev/null 2>/dev/null | tr -d '\r')"
dj_age="${dj%% *}"; dj_health="${dj##* }"
if [[ "$dj_age" == "$EXP_CONN_MAX_AGE" ]]; then
  pass "CONN_MAX_AGE = $dj_age"
else
  fail "CONN_MAX_AGE = '${dj_age:-?}' (expected $EXP_CONN_MAX_AGE)"
fi
if [[ "$dj_health" == "True" ]]; then
  pass "CONN_HEALTH_CHECKS = True"
else
  fail "CONN_HEALTH_CHECKS = '${dj_health:-?}' (expected True)"
fi

# --- 5. Pooling behaviour + connection budget (informational) --------------
hdr "Connection pool (live)"
if [[ -n "$($COMPOSE ps -q postgres 2>/dev/null)" ]]; then
  used="$(psql_t "select count(*) from pg_stat_activity")"
  oldest="$(psql_t "select coalesce(max(extract(epoch from now()-backend_start))::int,0) from pg_stat_activity where backend_type='client backend'")"
  info "connections in use: ${used:-?} / ${EXP_MAX_CONNECTIONS} max"
  if [[ -n "$oldest" && "$oldest" -gt 90 ]]; then
    pass "oldest client backend alive ${oldest}s → connections are reused, not per-request"
  else
    info "oldest client backend ${oldest:-?}s (run again after light traffic to see reuse)"
  fi
fi

# --- verdict ---------------------------------------------------------------
hdr "Result"
if [[ "$FAILED" -eq 0 ]]; then
  printf "  ${green}All production tuning is live.${rst}\n"
else
  printf "  ${red}One or more checks failed — see above.${rst}\n"
fi
exit "$FAILED"
