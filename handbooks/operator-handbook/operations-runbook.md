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
- [Combat Signatures](#combat-signatures)

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

## Combat Signatures

Combat Signatures are pilot-authored PNG banner images, pre-rendered off-request by a Celery
beat and served straight off disk by nginx. The feature ships **dark** (a leadership master
switch, off by default) and its render/rate-limit knobs are env-tunable. For the pilot- and
leadership-facing behaviour see the [Feature catalog](../feature-catalog.md#combat-signatures)
and [Leadership Features](../administrator-handbook/leadership-features.md#combat-signatures);
this section is the operator view.

### Storage and the media volume

Rendered banners live on the persistent `media_data` volume at
`MEDIA_ROOT/signatures/<token>.png`. In production that volume is mounted **read-write on the
`worker`** (the renderer writes there) and **read-only on `nginx`** (which only serves it) —
the same split the eveimg mirror uses. Files are written 0o644 in a 0o755 directory so the
read-only nginx uid can serve them.

Images are small and bounded: about **43 KiB median** per banner (min ~30 KiB, max ~65 KiB
for the busiest full-component design). One live artifact exists per active signature, so a
fully-saturated 200-pilot corp at the default quota of five is only **~42 MiB** of disk. This
is transient, regenerable data — see backups below.

### nginx

The prod nginx config (`deploy/nginx/forca.prod.conf`) carries a
`location ~ "^/s/(?<sig_token>…)\.png$"` block that serves the artifact straight from
`/srv/media/signatures/` with an `alias`, re-asserting the security headers plus
`X-Robots-Tag: noindex, nofollow` (static responses do not inherit server-level `add_header`).
A missing file — render still pending, or a disabled/rotated/unknown token — falls through
`error_page 404 = @sig_upstream` to the Django fallback view, which returns the correct status
(a 200 placeholder while pending; a constant-shape 404 for disabled or unknown). nginx-served
hits cost no gunicorn budget; only the fallback path touches the app, and it is per-IP
throttled. If you front FORCA with your own proxy/CDN, replicate that path.

### Image rebuild on deploy (CJK fonts)

The renderer's font chain is **DejaVu Sans** (Latin/Cyrillic) with **Noto Sans CJK**
per-glyph fallback for Chinese, Japanese, and Korean. Both are installed as Debian packages
(`fonts-dejavu-core`, `fonts-noto-cjk`) in the application image by the `Dockerfile`. A deploy
that changes the `Dockerfile` must **rebuild the image** (`make update` / step 4/7 of the safe
upgrade path already does this) — a stale image without `fonts-noto-cjk` renders CJK pilot
names and localised labels as tofu boxes. The renderer degrades gracefully (it falls back to
DejaVu when the CJK package is absent), so a banner still renders — just without CJK glyphs.

### Scheduled jobs

Two beat entries drive the feature (both inert until the master switch is armed — a single
cheap config read returns immediately):

| Task | Cadence | What it does |
|---|---|---|
| `killboard.signature_tick` | Every 10 min (`3-59/10`) | Advance the kill-stream cursor → mark touched pilots' live banners dirty, run the membership freeze/unfreeze sweep, and re-render a capped batch of due signatures. |
| `killboard.signature_cleanup` | Daily 04:13 UTC | Media janitor: delete artifacts with no row or for a disabled signature (disable/rotate already delete eagerly; this catches crash-orphaned files). |

The tick is coalesced (one render per signature per tick), debounced, globally mutexed against
overlapping beats, and hard-capped at `SIGNATURE_RENDER_MAX_PER_TICK` renders — a burst of
fresh kills can never storm the shared worker queue. At the default cap a full tick is on the
order of a second of worker time, a small minority of the 10-minute interval.

### Environment knobs

All are Django settings with env overrides (see `config/settings/base.py`); leadership-facing
options (enable, quota, refresh interval, revoke-on-leave) live on the console singleton
instead, not here.

| Setting | Default | Purpose |
|---|---|---|
| `SIGNATURE_RENDER_MAX_FAILURES` | `5` | After this many consecutive render failures the tick's picker parks a signature until its config changes or it is regenerated. Raise if a transient upstream causes flapping; lower to give up sooner. |
| `SIGNATURE_RENDER_MAX_PER_TICK` | `30` | Cap on banners re-rendered per 10-minute tick. Raise only if a large corp's refresh backlog never drains within the interval, mindful of worker budget. |
| `SIGNATURE_PUBLIC_RATE` | `120` | Per-IP requests/min for the public delivery **fallback** only (nginx-served hits bypass Django). `0` disables the throttle. |
| `SIGNATURE_PREVIEW_RATE` | `10` | Per-user requests/min for the in-builder synchronous preview (renders in-request). `0` disables it. |
| `SIGNATURE_REGENERATE_RATE` | `5` | Per-user requests/min for the manual regenerate action (each clears the debounce + failure ledger). `0` disables it. |

### Monitoring

The admin console **Combat Signatures dashboard** (`/ops/`, Officer) is the health surface:
status and render-status counts, the oldest pending render, parked failures (with the
admin-only render error a pilot never sees), recent failures, storage bytes and an orphan
estimate, the background catalogue summary, and a **provenance check** that re-hashes every
enabled background's committed files against the manifest (a mismatch is a tamper / bad-deploy
signal). Two Director maintenance buttons — *re-render all* and *clean up orphaned images* —
POST to the console maintenance-task launcher (`killboard.signature_rerender_all`,
`killboard.signature_cleanup`). *Re-render all* is a single bounded `UPDATE` that flags the
active set dirty; the tick then drains it at the per-tick cap, so it never enqueues N tasks or
storms the worker.

### Backup and restore

Backups **deliberately exclude the rendered images** — like the eveimg cache, they are
regenerable, not source of truth. Every signature's configuration lives in the database, so
after restoring a database into a host with an empty `media_data` volume, run **re-render all**
from the console (or wait for live signatures to refresh on the interval); the images rebuild
from the restored rows. See [Backup and Restore](./backup-and-restore.md).
