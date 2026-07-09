# Upgrades

How to safely upgrade a running [FORCA] Command Grid installation to a newer version.

## Table of contents

- [The safe upgrade path](#the-safe-upgrade-path)
- [Re-running the deploy script](#re-running-the-deploy-script)
- [Migration procedure](#migration-procedure)
- [Rollback considerations](#rollback-considerations)

## The safe upgrade path

```bash
make update
```

This runs `scripts/update.sh`, which performs the following steps **in order**, aborting
on the first failure so a partial upgrade never leaves the stack in an unknown state:

| Step | Action |
|---|---|
| 1/6 | **Back up the database** (`scripts/backup.sh ./backups`) — aborts the entire upgrade if the backup fails |
| 2/6 | `git fetch --prune origin`, `git checkout <branch>`, then `git pull --ff-only origin <branch>` — **fast-forward only**; refuses to proceed if your checkout has diverged (e.g. local changes) rather than silently discarding anything |
| 3/6 | Stamp the new build revision (`deploy/stamp-version.sh`) so the footer reflects what's actually deployed |
| 4/6 | Rebuild and restart the stack (`docker compose -f docker-compose.prod.yml up -d --build`) |
| 5/6 | Wait for services to become ready (`scripts/wait-for-services.sh`), then apply migrations and `collectstatic` |
| 6/6 | Run the health check (`scripts/healthcheck.sh`) — a failure here is reported as a warning, not aborted, since the upgrade steps themselves already completed; investigate immediately if it fails |

By default it upgrades the **currently checked-out branch**; pass a branch explicitly if
needed:

```bash
scripts/update.sh main
```

Because the backup happens first and unconditionally, `make update` is the recommended
way to upgrade in all cases — it is strictly safer than manually rebuilding and
migrating.

## Re-running the deploy script

`deploy/deploy-ubuntu-26.04.sh` is **idempotent**: re-running the exact command you used
for the initial install (see [Deployment](./deployment.md#path-a-the-one-shot-script))
performs an in-place upgrade — it fetches/checks out/fast-forwards the repository,
rebuilds, and re-runs migrations, **without** overwriting your existing `.env` or
regenerating secrets. This is a reasonable alternative to `make update` if you also want
to re-apply host-level provisioning (firewall rules, `fail2ban`, `unattended-upgrades`
configuration) at the same time, or if the systemd unit / cron job needs to be
re-installed.

Note that the script does **not** take an explicit pre-upgrade database backup the way
`scripts/update.sh` does — if you use this path for a routine upgrade, take a manual
`make backup` first, or rely on the nightly backup cron already in place from the
initial install.

## Migration procedure

Database schema migrations are applied automatically by both upgrade paths (`make
update` and the deploy script), via:

```bash
docker compose -f docker-compose.prod.yml exec -T web python manage.py migrate --noinput
```

To apply migrations on their own at any time (e.g. after a manual `git pull` and
rebuild):

```bash
make migrate
```

`scripts/healthcheck.sh` (and therefore `make health`) checks for **unapplied
migrations** as one of its health checks — if an upgrade step is ever skipped, the next
health check will flag it rather than let the discrepancy go unnoticed.

## Rollback considerations

Use `scripts/rollback.sh`. Because `scripts/update.sh` always dumps the database as step
1/6 — before any code or schema change — the ingredients for a rollback exist for
**every** upgrade performed through `make update`.

```bash
# See what you can roll back to
git log --oneline -10
ls -1t ./backups/*.sql.gz

# Code only: fast, keeps data written since the upgrade
make rollback REF=v1.0.0

# Code + database: the only fully consistent option
make rollback REF=v1.0.0 DUMP=./backups/forca-20260709-031500.sql.gz
```

The script refuses to run with uncommitted changes, takes a fresh safety backup of the
**current** database first (so the rollback is itself reversible), records the revision
you came from in `.rollback-from`, rebuilds, and runs a health check. It leaves you on a
detached HEAD; `git checkout main` returns to the branch tip.

### Choosing whether to restore the database

Django does **not** un-apply migrations here. If the upgrade you are undoing added
migrations, the schema is newer than the code you are rolling back to.

| Situation | What to do |
| --- | --- |
| The upgrade added no migrations | `make rollback REF=...` — code only |
| It added only *additive* migrations (new nullable column, new table) | Code only usually works; the old code simply ignores the new objects |
| It *altered* or *dropped* columns, or changed constraints | You must pass `DUMP=` — old code against the new schema will fail in ways that are hard to diagnose |

Restoring the database **discards every row written since that dump was taken** (new
killmails, SRP claims, ledger entries). That is the price of a self-consistent state.
When in doubt, restore: an inconsistent schema is worse than a few hours of lost sync
data, and the syncs will re-fetch from ESI.

### Doing it by hand

If you would rather not use the script, the same four steps are:

1. `scripts/backup.sh ./backups` — safety net for the rollback itself.
2. `git checkout <previous-ref>` and `deploy/stamp-version.sh .`
3. `docker compose -f docker-compose.prod.yml up -d --build`
4. `make restore FILE=./backups/<pre-upgrade-dump>.sql.gz` (only if the schema moved),
   then `make health`.
