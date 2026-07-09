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
