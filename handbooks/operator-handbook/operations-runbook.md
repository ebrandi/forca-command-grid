# Operations Runbook

Recurring checklists for running [FORCA] Command Grid day to day, using the `Makefile`
command surface. Pair this page with
[Monitoring and Health](./monitoring-and-health.md) for what "healthy" means and
[Troubleshooting](./troubleshooting.md) for when something isn't.

## Table of contents

- [Daily checklist](#daily-checklist)
- [Weekly checklist](#weekly-checklist)
- [Monthly checklist](#monthly-checklist)
- [Scheduled jobs and workers](#scheduled-jobs-and-workers)
- [Ad hoc operations](#ad-hoc-operations)
- [Realtime killboard stream (KB-29)](#realtime-killboard-stream-kb-29)

## Daily checklist

- [ ] `make health` — confirm containers are healthy, `/healthz` returns 200, no
      unapplied migrations, Redis responds, and the Celery worker responds to a ping.
- [ ] `make ps` — a quick glance at container status and restart counts.
- [ ] Check `/ops/health/` (Director role required) — token health per linked character,
      per-feed sync freshness, and per-beat last-success age. This is the fastest way to
      spot an expired Director token or a stalled sync before anyone notices missing data.
- [ ] Skim `make logs` (or `docker compose -f docker-compose.prod.yml logs --tail=100
      <service>` for a specific service) for unexpected errors, especially after any
      manual change.

## Weekly checklist

- [ ] Confirm the nightly backup cron is producing dumps: check `./backups` (manual
      path) or the automated script's backup directory (`/var/backups/forca` by
      default) for a recent, non-trivial file. See
      [Backup and Restore](./backup-and-restore.md).
- [ ] Review the weekly dependency-vulnerability scan result. `admin_audit.audit_dependencies`
      runs every Monday at 06:30 UTC (matching the CI `security.yml` workflow) and raises
      a Director-visible finding on any newly disclosed CVE in `requirements.txt`.
- [ ] Review any open findings raised by `admin_audit.scan_integration_health` (deduped
      Director alert when a background sync stops, the SDE goes stale, or a dependency
      CVE appears — runs every 30 minutes).
- [ ] Check TLS certificate expiry if you're not fully relying on `certbot.timer`:
      `sudo certbot certificates`, or `openssl s_client -connect <domain>:443 -servername
      <domain> 2>/dev/null | openssl x509 -noout -enddate`.
- [ ] Check disk usage: `df -h` and `docker system df -v` — the PostgreSQL volume
      (`pg_data`) and the EVE image mirror (`eveimg_data`) are the largest, growing
      consumers.

## Monthly checklist

- [ ] Confirm the OS is current: `unattended-upgrades` handles security patches
      automatically on hosts provisioned by the deploy script; verify with
      `sudo unattended-upgrade --dry-run -d` or check `/var/log/unattended-upgrades/`.
- [ ] Pull fresh base images and rebuild, to pick up upstream security patches in
      `python:3.12-slim`, `postgres:16-alpine`, `redis:7-alpine`, and `nginx:1.27-alpine`:
      `docker compose -f docker-compose.prod.yml pull` followed by a normal
      `make update` or `make deploy`.
- [ ] Confirm you can actually restore a backup in a scratch environment (see
      [Backup and Restore § Disaster recovery considerations](./backup-and-restore.md#disaster-recovery-considerations)) —
      an untested backup is not a verified backup.
- [ ] Review Admin Console access: who holds Officer/Director roles and lateral
      capabilities (see [Permissions and Roles](../permissions-and-roles.md)), and
      whether any should be revoked.
- [ ] Review `NOTICE.md` / `pip-audit` output for any dependency licence or
      vulnerability items needing attention beyond the automated weekly scan.

## Scheduled jobs and workers

All recurring application work — killmail imports, price syncs, cache warming,
notifications, retention housekeeping, and roughly 90 other tasks — is scheduled by the
`beat` container and executed by the `worker` container; no host cron is involved for
application logic (the **only** host cron the deploy script installs is the nightly
database backup, described in [Backup and Restore](./backup-and-restore.md)).

The full task-by-task reference — cadence and what each one does — is documented
separately: **[Background Jobs Reference](../reference/background-jobs.md)**. Two
operational facts worth repeating here:

- If `beat` is down, **nothing is scheduled** — no new tasks are enqueued at all.
- If `worker` is down, tasks **queue in Redis** and run once it returns — no work is
  lost, but data goes stale in the meantime.

Confirm both are running and registering tasks:

```bash
docker compose -f docker-compose.prod.yml ps beat worker
docker compose -f docker-compose.prod.yml exec worker celery -A config inspect registered
docker compose -f docker-compose.prod.yml logs -f beat worker
```

## Ad hoc operations

| Task | Command |
|---|---|
| Tail all service logs | `make logs` |
| Tail one service | `docker compose -f docker-compose.prod.yml logs -f <service>` |
| Container status | `make ps` |
| Restart the stack | `make restart` |
| Stop the stack (data preserved) | `make down` |
| Django shell | `make shell` |
| `psql` shell | `make dbshell` |
| Ensure a superuser exists | `make create-admin EMAIL=you@example.com` |
| Re-import the SDE | `make import-sde` |
| Re-mirror referenced type images | `make import-assets` |
| Refresh Jita prices | `make prices` |
| Manual backup | `make backup` |
| Restore from a dump | `make restore FILE=./backups/forca-....sql.gz` (see [Backup and Restore](./backup-and-restore.md)) |
| Obtain/renew TLS | `sudo make cert DOMAIN=... EMAIL=...` |
| Validate both compose files parse | `make config-check` |

## Realtime killboard stream (KB-29)

The board can push new home-corp kills to browsers and integrations in real time over
Server-Sent Events (SSE) at `GET /api/killboard/stream/`, with a JSON short-poll fallback
(`?mode=poll`) over the same cursor. It powers the "LIVE" killfeed on the board landing page
and can feed Discord bots or dashboards. Auth mirrors the REST API (session or bearer token;
anonymous only when `KILLBOARD_API_PUBLIC_READ` is on, and then only public topics).

### Worker-budget assessment (read before raising limits)

A long-lived SSE response **occupies one gunicorn `gthread` thread for its whole lifetime**.
Production runs `GUNICORN_WORKERS` (default 3) × `GUNICORN_THREADS` (default 4) = **12
concurrent request slots for the entire suite**. Streaming is therefore deliberately bounded so
it cannot starve the site:

- **Connection cap** — `KILLBOARD_STREAM_MAX_CLIENTS` (default **4**, a third of the pool at
  worst). When full the endpoint returns `503` + `Retry-After` and the client automatically
  degrades to short-polling, so extra viewers are never broken — only slightly less live.
- **Bounded lifetime** — every stream auto-closes after `KILLBOARD_STREAM_MAX_LIFETIME_S`
  (default 120s); the browser reconnects transparently via `Last-Event-ID`. This caps how long a
  thread is held and lets a wedged slot recycle.
- **Heartbeat** — a comment every `KILLBOARD_STREAM_HEARTBEAT_S` (default 15s) keeps the
  connection and nginx's read timeout alive and detects dead clients.
- **Poll fallback** — `?mode=poll` holds a thread only for one indexed query, so it scales far
  past the SSE cap; it is the cheaper shape for bots and the automatic browser fallback.

**Golden rule: keep `KILLBOARD_STREAM_MAX_CLIENTS` a minority of the thread pool.** If you
expect many simultaneous live viewers, first raise `GUNICORN_THREADS` / `GUNICORN_WORKERS`
(sizing RAM accordingly), *then* raise the cap. If you would rather not spend threads on
streaming at all, set `KILLBOARD_STREAM_MAX_CLIENTS=0` — SSE then always 503s and every client
uses the cheap poll path — or `KILLBOARD_STREAM_ENABLED=false` to turn the feature (and the
LIVE badge) off entirely.

### Settings (all env-overridable)

| Setting | Default | Purpose |
|---|---|---|
| `KILLBOARD_STREAM_ENABLED` | `true` | Master switch for the endpoint + the LIVE badge. |
| `KILLBOARD_STREAM_MAX_CLIENTS` | `4` | Concurrent SSE cap (Redis semaphore). Keep < thread pool. |
| `KILLBOARD_STREAM_HEARTBEAT_S` | `15` | Heartbeat/keep-alive interval. |
| `KILLBOARD_STREAM_MAX_LIFETIME_S` | `120` | Hard per-connection lifetime before client resumes. |
| `KILLBOARD_STREAM_POLL_INTERVAL_S` | `2` | Server-side event-check cadence within a stream. |
| `KILLBOARD_STREAM_FRESH_HOURS` | `48` | Only kills newer than this are emitted (backfill never floods the feed). |
| `KILLBOARD_STREAM_BATCH` | `200` | Max events served per flush/poll. |
| `KILLBOARD_STREAM_RETENTION` | `10000` | Ring-buffer size kept by the hourly prune. |

### nginx

The prod nginx config (`deploy/nginx/forca.prod.conf`) already carries an exact-match
`location = /api/killboard/stream/` block with `proxy_buffering off`, `proxy_cache off` and a
`proxy_read_timeout` (135s) just above the app's bounded lifetime — SSE will not work through a
buffering proxy. If you front FORCA with your own proxy/CDN, replicate those three settings for
that path. The app also sends `X-Accel-Buffering: no` as a backstop.

### Housekeeping

The ring buffer is trimmed to `KILLBOARD_STREAM_RETENTION` rows hourly by the
`killboard.prune_stream_events` beat task (minute 34). The stream is a live feed, not history —
the board, the REST API and EVE Ref remain the durable record — so a trimmed or briefly
unavailable stream loses nothing.
