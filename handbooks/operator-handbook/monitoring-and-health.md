# Monitoring and Health

How [FORCA] Command Grid reports its own health, at the container, application, and
integration level, and what to watch for continuous monitoring.

## Table of contents

- [The `/healthz` endpoint](#the-healthz-endpoint)
- [`scripts/healthcheck.sh`](#scriptshealthchecksh)
- [Docker Compose healthchecks](#docker-compose-healthchecks)
- [The `/ops/health/` page](#the-opshealth-page)
- [Log locations and interpretation](#log-locations-and-interpretation)
- [Recommended alerting](#recommended-alerting)

## The `/healthz` endpoint

`GET /healthz` ([`config/views.py`](../../config/views.py)) is a liveness/readiness
probe: it runs `SELECT 1` against the database and returns JSON.

```json
{"status": "ok", "database": true}
```

| Condition | HTTP status | `status` |
|---|---|---|
| Database reachable | 200 | `"ok"` |
| Database query fails | 503 | `"degraded"` |

This endpoint is used by:

- The **Docker healthcheck** on the `web` container (see below).
- `scripts/wait-for-services.sh` during deploy/update, to know when `web` is ready.
- `scripts/healthcheck.sh`.

It is intentionally exempt from the HTTPS redirect (`SECURE_REDIRECT_EXEMPT` in
`config/settings/prod.py`), since it's checked over plain HTTP on `127.0.0.1` from inside
the container, without a proxy header.

## `scripts/healthcheck.sh`

Run directly (`make health`) or as part of every deploy/update, this script checks, in
order:

| Check | How |
|---|---|
| Container status | `docker compose ... ps` (informational table) |
| Web `/healthz` responds 200 | `python -c` inside the `web` container |
| Database reachable | `manage.py showmigrations --list` succeeds |
| No unapplied migrations | `manage.py showmigrations --plan` has no `[ ]` entries |
| Redis responds | `redis-cli -a "$REDIS_PASSWORD" ping` returns `PONG` |
| Celery worker responds | `celery -A config inspect ping` |
| SDE reference data loaded | The `sde_version` `AppSetting` key exists (stamped by the SDE importer) |

It exits non-zero and prints which check(s) failed if anything is unhealthy — this is
the first command to run in any investigation (see
[Troubleshooting](./troubleshooting.md)).

## Docker Compose healthchecks

`docker-compose.prod.yml` defines container-level healthchecks independent of the script
above, so `docker compose ps` and the `depends_on: condition: service_healthy` chain
(e.g. `web`/`worker`/`beat` waiting on `postgres` and `redis`) reflect real readiness:

| Container | Healthcheck | Interval |
|---|---|---|
| `web` | `GET http://127.0.0.1:8000/healthz` returns 200 | 30s (40s start period, 5 retries) |
| `postgres` | `pg_isready -U $POSTGRES_USER -d $POSTGRES_DB` | 10s (5 retries) |
| `redis` | `redis-cli -a $REDIS_PASSWORD ping` returns `PONG` | 10s (5 retries) |

`worker`, `beat`, and `nginx` have no declared Docker healthcheck; monitor them via
`scripts/healthcheck.sh` (Celery ping) and log inspection.

## The `/ops/health/` page

Beyond container/process health, the application surfaces **integration health** —
whether each ESI data feed is actually current — at `/ops/health/` (Director role
required). It reports, from `apps/admin_audit/health.py`:

- **Token health** — per linked, non-revoked character: which key ESI scopes it holds,
  whether its access token is expired or revoked, and its refresh success/failure
  history.
- **Feed health** — last-sync time, record count, and freshness status (`ok` / `stale` /
  `missing`) for corp assets, personal assets, market history, killmails, member
  roster, member skills, and market prices.
- **SDE health** — the loaded Static Data Export version and how long ago it was loaded
  (a soft "refresh due" appears after roughly 45 days, matching CCP's patch cadence).
- **Beat health** — the last-success age for every recorded scheduled-job stamp,
  surfacing a silently-stopped beat in one place.

This page is also summarized by the scheduled `admin_audit.scan_integration_health` task
(every 30 minutes), which raises a single, deduped Director-facing alert when a
background sync stops, the SDE goes stale, or a dependency vulnerability is found — so
you don't have to watch the page continuously to catch a regression. See
[Background Jobs Reference](../reference/background-jobs.md#comms-access-doctrines-and-housekeeping).

## Log locations and interpretation

- All application logs (`web`, `worker`, `beat`) go to **stdout**, captured by Docker.
  View them with `docker compose -f docker-compose.prod.yml logs -f <service>` or
  `make logs` for everything.
- The root log level is controlled by `DJANGO_LOG_LEVEL` (`INFO` by default) —
  see [Configuration Reference](../configuration-reference.md#django-core).
- Logs may contain character/corporation identifiers, request paths, and error context.
  Secrets are not intentionally logged; the ESI and LLM clients redact credentials (see
  [Data and Privacy § Logs and data](../data-and-privacy.md#logs-and-data)).
- `nginx` logs (via `docker compose logs nginx`) show edge-level activity: rate-limit
  `429`/`503` responses, bare-IP `444` drops, and the AI/SEO crawler block on faceted
  killboard URLs.
- Docker logs are **not persisted across container recreation** by default in this
  compose file — if you need durable log retention, ship stdout to a log aggregator at
  the Docker daemon level (e.g. a `json-file` size cap, or a logging driver of your
  choice) rather than relying on `docker compose logs` history.

## Recommended alerting

The application ships no external monitoring agent — the following are **recommended**
practices for an operator, not enforced by the software:

- Poll `/healthz` from an external uptime monitor and alert on non-200 responses or
  timeouts.
- Run `scripts/healthcheck.sh` on a schedule (e.g. a systemd timer or external cron) and
  alert on non-zero exit.
- Watch the `/ops/health/` page or its data (query the `AppSetting` table / the
  `integration_health()` helper) for `stale` or `missing` feeds, since a stopped sync
  degrades data quality without crashing anything.
- Alert on disk usage thresholds for the Docker host (`pg_data` and `eveimg_data` are the
  fastest-growing volumes).
- Alert on TLS certificate expiry independent of `certbot.timer`, as a second layer of
  defense against a stuck renewal.
- Alert on OOM-killed containers (`docker inspect -f '{{.State.OOMKilled}}'
  <container>`) — see [Troubleshooting](./troubleshooting.md).
