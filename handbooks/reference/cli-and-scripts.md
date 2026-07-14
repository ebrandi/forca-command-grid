# CLI and Scripts Reference

This page lists the operator command surface (`Makefile` targets and `scripts/`), the Django
management commands, and the deployment scripts. All commands run against the containerised
stack.

## Table of contents

- [Makefile targets](#makefile-targets)
- [Operator scripts](#operator-scripts)
- [Deployment scripts](#deployment-scripts)
- [Django management commands](#django-management-commands)

## Makefile targets

Run `make help` to list every target. Production targets use `docker-compose.prod.yml`;
development targets use `docker-compose.yml`.

| Target | Purpose |
|---|---|
| `make setup` | Create `.env` from the template if it does not exist |
| `make build` | Build the production images |
| `make deploy` | Build + start the prod stack, then migrate and collectstatic |
| `make update` | Pull latest code, rebuild, migrate (safe upgrade path) |
| `make migrate` | Apply database migrations |
| `make collectstatic` | Collect static assets |
| `make bootstrap` | Load EVE reference data (full SDE + PI + referenced images) |
| `make bootstrap-sample` | Load the tiny bundled sample SDE (dev/CI only) |
| `make import-sde` | Import the full Static Data Export from Fuzzwork |
| `make import-assets` | Mirror referenced EVE type images locally |
| `make prices` | Price referenced types from Jita (first pass) |
| `make create-admin` | Ensure a Django superuser (`EMAIL=you@example.com`) |
| `make health` | Run the full health check |
| `make logs` | Tail logs for all prod services |
| `make ps` | Show prod container status |
| `make restart` | Restart the prod stack |
| `make down` | Stop the prod stack (data volumes preserved) |
| `make shell` | Open a Django shell in the web container |
| `make dbshell` | Open a `psql` shell |
| `make backup` | Dump the database to `./backups` |
| `make restore` | Restore the DB from a dump (`FILE=./backups/forca-....sql.gz`) |
| `make cert` | Obtain/renew TLS cert (`DOMAIN=... EMAIL=...`; run with sudo) |
| `make config-check` | Validate the compose files parse |
| `make dev` / `make dev-down` / `make dev-logs` | Local development stack lifecycle |

## Operator scripts

Located in [`scripts/`](../../scripts). All source `scripts/lib.sh` and never print secrets.

| Script | Purpose |
|---|---|
| `backup.sh [output_dir]` | Timestamped gzipped `pg_dump` with an integrity check; keeps the newest `BACKUP_KEEP` dumps (default 14). |
| `restore.sh <dump.sql.gz> [--yes]` | Destructive restore: takes a safety backup, drops/recreates the schema, restores, then migrates. Confirmation-gated. |
| `update.sh [branch]` | Safe upgrade: backup → fast-forward pull → stamp → rebuild → migrate → collectstatic → health. |
| `rollback.sh <git-ref> [--restore <dump.sql.gz>] [--yes]` | Return to an earlier revision. Refuses a dirty tree, takes a safety backup of the current DB, records the previous revision in `.rollback-from`, rebuilds, health-checks. Pass `--restore` when the upgrade altered the schema. Confirmation-gated. |
| `bootstrap-data.sh [--sample] [--all-images] [--no-images] [--with-prices]` | Load EVE reference data (SDE, PI rulebook, images, optional prices). Idempotent. |
| `healthcheck.sh` | Read-only stack health: container states, web `/healthz`, DB + migrations, Redis PING, Celery worker ping, SDE loaded. |
| `cert-init.sh <domain> <email> [app_dir]` | Obtain a Let's Encrypt certificate (certbot standalone) and wire up automatic renewal. Run as root. |
| `create-admin.sh <email>` | Ensure a Django superuser exists. |
| `wait-for-services.sh` | Block until the stack's services are ready (used by deploy/update). |
| `lib.sh` | Shared logging, validation, and compose-resolution helpers. |
| `perf/*.py` | Performance helpers (index checks, endpoint timing, cache warming). |

## Deployment scripts

Located in [`deploy/`](../../deploy).

| Script | Purpose |
|---|---|
| `deploy-ubuntu-26.04.sh` | One-shot, idempotent host provisioning + deploy for a fresh Ubuntu 24.04 LTS+ server (run as root). |
| `stamp-version.sh` | Materialise the deployed commit hash into `.git-commit` (footer build marker). |
| `verify-prod.sh` | Post-deploy sanity checks against a running stack. |

## Django management commands

Run with `docker compose -f docker-compose.prod.yml exec web python manage.py <command>`
(or via the `make` wrappers above). Commands are provided by the apps; the data importers
are idempotent (they upsert and skip existing rows/files).

| Command | Purpose |
|---|---|
| `import_sde_fuzzwork` | Import the full Static Data Export from a Fuzzwork dump. Supports scoped flags (e.g. `--blueprints-only`, `--coords-only`, `--skill-attrs-only`). |
| `load_sde` | Load the tiny bundled sample SDE fixture (dev/CI). |
| `load_pi_static` | Load the Planetary Industry rulebook. |
| `import_everef_reference_data` | Backfill reference data (e.g. packaged volumes) from EveRef. |
| `mirror_type_images` | Mirror EVE type icons/renders to the local nginx-served directory (`--referenced-only` for the fast path). |
| `price_types` | Price referenced types from Jita (first pass). |
| `import_adjusted_prices` | Import CCP adjusted/average reference prices. |
| `import_everef_market_history` | Backfill market history from EveRef archives. |
| `import_zkill` / `import_zkill_history` | Import corp killmails from zKillboard (current / historical). |
| `import_everef_killmails` | Backfill killmails from EveRef daily archives. |
| `import_everef_contracts` | Import EveRef public-contracts snapshot for the freight rate benchmark. |
| `revalue_killmails` | Re-value killmails after a price refresh. |
| `retag_doctrine_fits` | Re-tag doctrine-matched losses. |
| `backfill_monthly_stats` | Fill the per-pilot monthly ranking aggregates (one-time backfill). |
| `backfill_raffle_names` | Backfill entity names for raffle records. |
| `audit_dependencies` | Run the `pip-audit` dependency vulnerability scan. |
| `rollback_safety` | Check whether a code-only rollback is safe against the current schema (called by `scripts/rollback.sh`). Exit 0 = safe, 1 = not. |
| `seed_demo` | Seed roles, a home corp, and a demo doctrine (dev). |
| `seed_examples` | Seed example content (dev). |

There is no custom command for localisation. The message catalogues are compiled into the
image at build time by stock `python manage.py compilemessages`, which both the
[`Dockerfile`](../../Dockerfile) and CI run, so a malformed `.po` fails the build instead of
falling back to English at runtime, and an operator has no compile step to run. Contributors
re-extract with `make messages` and compile locally with `make compile-messages`. The `.po`
files under `locale/` are tracked; `.mo` files are build output and are not committed, and
`.dockerignore` excludes `locale/**/*.mo` so a stale one in the build context cannot make
`compilemessages` skip silently.

For the scheduled equivalents of the sync/price/import jobs, see
[background-jobs.md](./background-jobs.md).
