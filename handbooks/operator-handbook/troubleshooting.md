# Troubleshooting

A symptom-first reference for common [FORCA] Command Grid operational problems. Start
every investigation with:

```bash
make health
make ps
docker compose -f docker-compose.prod.yml logs --tail=100 <service>
```

## Table of contents

- [Quick reference table](#quick-reference-table)
- [502 after redeploy](#502-after-redeploy)
- [`DisallowedHost` in the logs](#disallowedhost-in-the-logs)
- [TLS / certificate renewal problems](#tls--certificate-renewal-problems)
- [SDE not loaded (raw IDs in the UI)](#sde-not-loaded-raw-ids-in-the-ui)
- [Celery worker not running](#celery-worker-not-running)
- [Beat is down](#beat-is-down)
- [Redis memory eviction](#redis-memory-eviction)
- [ESI throttling / User-Agent](#esi-throttling--user-agent)
- [Migrations pending](#migrations-pending)
- [Healthcheck failing](#healthcheck-failing)
- [Starting over (destructive)](#starting-over-destructive)

## Quick reference table

| Symptom | Likely cause | Action |
|---|---|---|
| 502/504 from nginx right after a redeploy | nginx holding a stale upstream IP for the recreated `web` container | `docker compose -f docker-compose.prod.yml up -d --force-recreate nginx` |
| `Invalid HTTP_HOST header` / `DisallowedHost` | A scanner hit the bare IP (harmless — nginx already drops it at the edge), or your real domain is missing from `DJANGO_ALLOWED_HOSTS` | Add the domain to `DJANGO_ALLOWED_HOSTS` and restart; confirm nginx's bare-IP `444` rule is intact |
| TLS issuance/renewal fails | DNS doesn't point at this host, or port 80 isn't reachable during the standalone challenge | Confirm the A/AAAA record and that `ufw`/upstream firewall allow 80/443 |
| UI shows raw numeric IDs instead of ship/system/skill names | The SDE hasn't been imported | `make import-sde` (or `make bootstrap`); confirm with `make health` |
| Celery tasks pile up / nothing processes | The `worker` container is down or unhealthy | `docker compose -f docker-compose.prod.yml ps worker`; check its logs; restart it |
| Nothing is scheduled at all, even routine syncs | The `beat` container is down | `docker compose -f docker-compose.prod.yml ps beat`; check its logs; restart it |
| Redis evicting keys / cache misses climbing | `maxmemory 512mb` reached; `volatile-lru` is evicting TTL-bearing cache keys (by design) | Expected behavior under pressure — confirm the Celery broker (no-TTL keys) is unaffected; raise `--maxmemory` in `docker-compose.prod.yml` if it's chronic |
| ESI calls throttled (420/429) | Generic/blank `ESI_USER_AGENT`, or the ESI error budget is genuinely exhausted | Set a real contact in `ESI_USER_AGENT`; the client backs off automatically on 420/429 |
| `make health` reports unapplied migrations | A deploy/upgrade step was interrupted before migrating | `make migrate`; investigate why it didn't run automatically |
| `scripts/healthcheck.sh` fails | Any one of: web `/healthz`, DB, Redis, Celery worker, or SDE check | Read which specific check failed (the script names it) and jump to the matching section below |

## 502 after redeploy

**Cause:** nginx resolves `web` once and can hold a stale container IP briefly after
`web` is recreated (e.g. by `make deploy` / `make update`), because Docker's embedded DNS
entry changed but nginx's existing upstream connection didn't yet.

**Fix:**

```bash
docker compose -f docker-compose.prod.yml up -d --force-recreate nginx
```

Recreate `web` **before** reloading/recreating nginx during any manual deploy sequence,
to avoid this in the first place — this is why `make deploy` and `scripts/update.sh`
bring the whole stack up together with `up -d --build` rather than recreating services
one at a time.

## `DisallowedHost` in the logs

**Cause:** either a port scanner/uptime bot hitting the server's bare IP address
directly (harmless — the nginx config already returns `444` for a bare-IP `Host` header
before the request reaches Django), or your actual domain is missing from
`DJANGO_ALLOWED_HOSTS`.

**Fix:** if real users are affected, add the domain to `DJANGO_ALLOWED_HOSTS` in `.env`
and apply with `docker compose -f docker-compose.prod.yml up -d`. If it's only
scanner noise at the IP level, no action is needed — nginx already discards those before
Django logs them at all; occasional stray log lines from other paths are expected
internet background noise.

## TLS / certificate renewal problems

**Cause (issuance):** certbot runs in **standalone** mode, which needs port 80 free and
reachable from the internet during the HTTP-01 challenge. DNS not yet pointing at the
host, or the firewall blocking port 80, are the two common causes.

**Cause (renewal):** the same requirement applies to renewal — `certbot.timer` runs
`certbot renew`, whose pre-hook stops the nginx container to free port 80 and whose
post-hook restarts it; if nginx fails to restart, the site goes down until it's fixed.

**Fix:**

```bash
sudo certbot certificates             # check current cert status/expiry
sudo certbot renew --dry-run          # test the renewal chain without issuing
sudo systemctl status certbot.timer   # confirm the timer is enabled and running
```

Bring-your-own-certificate: place `forca.crt` (fullchain) and `forca.key` in `./certs`
and recreate nginx (`docker compose -f docker-compose.prod.yml up -d nginx`) — no
certbot involvement needed. See [Requirements](./requirements.md) and
[Deployment](./deployment.md) for the TLS model.

## SDE not loaded (raw IDs in the UI)

**Cause:** the Static Data Export (type/system/region/skill names) hasn't been imported
yet — expected immediately after a fresh install with `--skip-bootstrap`, or if
`make bootstrap` was never run.

**Fix:**

```bash
make import-sde        # or: make bootstrap (SDE + PI + referenced images)
make health             # should report "SDE reference data loaded"
```

## Celery worker not running

**Symptom:** tasks queue in Redis but nothing processes them; scheduled syncs never
complete; `/ops/health/` shows feeds going stale.

**Fix:**

```bash
docker compose -f docker-compose.prod.yml ps worker
docker compose -f docker-compose.prod.yml logs --tail=100 worker
docker compose -f docker-compose.prod.yml up -d worker    # restart if stopped/crashed
docker compose -f docker-compose.prod.yml exec worker celery -A config inspect ping
```

Because `worker` and `beat` share the application image and `.env`, a boot-time
configuration error (e.g. a bad `LLM_BASE_URL`) that crashes one will typically crash
both — check the logs for a Python traceback near startup.

## Beat is down

**Symptom:** **no** scheduled task runs at all — not just slow syncs, but a complete
absence of new activity across every subsystem, since nothing is being enqueued.

**Fix:**

```bash
docker compose -f docker-compose.prod.yml ps beat
docker compose -f docker-compose.prod.yml logs --tail=100 beat
docker compose -f docker-compose.prod.yml up -d beat
```

`admin_audit.scan_integration_health` (run by `worker`, scheduled by `beat` itself) is
designed to catch a stopped beat and alert a Director — but if `beat` itself is down,
that very task won't fire either, so don't rely on it exclusively; include `beat`'s
container status in routine monitoring (see
[Monitoring and Health](./monitoring-and-health.md)).

## Redis memory eviction

**Cause:** Redis is configured with `--maxmemory 512mb --maxmemory-policy volatile-lru`
— by design, once memory pressure hits that ceiling, Redis evicts the **least-recently-used
key that carries a TTL**. Every cache entry the application sets carries a bounded TTL;
the Celery broker's queue keys do not, so the broker is never evicted under this policy
— only cached, recomputable values are.

**This is expected, self-healing behavior**, not necessarily a problem: evicted cache
entries are simply recomputed on next access. It becomes worth acting on if eviction is
constant and cache hit rates are visibly suffering. Options:

- Raise `--maxmemory` in the `redis` service command in `docker-compose.prod.yml` (and
  its `mem_limit` backstop to match).
- Confirm nothing is caching unusually large values (check for a new feature bypassing
  the standard bounded-TTL cache pattern).

## ESI throttling / User-Agent

**Cause:** CCP's ESI may throttle or reject requests carrying a generic or blank
`User-Agent`, or genuinely rate-limits under its error budget (HTTP 420) or per-endpoint
limits (HTTP 429).

**Fix:** set a real, identifying contact in `ESI_USER_AGENT` (e.g.
`forca-command-grid/1.0 (you@example.com)`). The application's ESI client already
honors CCP's error budget and backs off automatically on 420/429 — persistent
throttling despite a real User-Agent typically means the error budget is being
exhausted by request volume, not a configuration problem; check `/ops/health/` for which
feed is generating the volume.

## Migrations pending

**Symptom:** `scripts/healthcheck.sh` / `make health` reports unapplied migrations.

**Cause:** a deploy or upgrade step was interrupted, or code was pulled without
following the [upgrade procedure](./upgrades.md).

**Fix:**

```bash
make migrate
```

If a specific migration fails with an error, read the full traceback in the `web`
container's logs — do **not** work around it by faking a migration state. If a partial
migration has left the schema in an inconsistent state, restore from the pre-upgrade
backup (see [Backup and Restore](./backup-and-restore.md) and
[Upgrades § Rollback considerations](./upgrades.md#rollback-considerations)).

## Healthcheck failing

`scripts/healthcheck.sh` names exactly which check failed — treat its output as your
starting point rather than guessing:

| Failed check | Jump to |
|---|---|
| `web /healthz responds 200` | Check `web` logs for a database connection error or an unhandled exception |
| `database reachable` | Confirm `postgres` is healthy and `DATABASE_URL` matches `POSTGRES_PASSWORD` |
| `no unapplied migrations` | [Migrations pending](#migrations-pending) |
| `redis PING` | Confirm `redis` is healthy and `REDIS_URL`/`CELERY_BROKER_URL` include the correct password |
| `celery worker responds` | [Celery worker not running](#celery-worker-not-running) |
| `SDE reference data loaded` | [SDE not loaded](#sde-not-loaded-raw-ids-in-the-ui) |

## Starting over (destructive)

```bash
docker compose -f docker-compose.prod.yml down -v   # deletes ALL named volumes, including pg_data
```

This is intentionally destructive — use it only when you mean to wipe the instance
entirely (e.g. tearing down a test deployment). A plain `docker compose ... down`
(without `-v`) stops the stack and **preserves** all data volumes.
