# Deployment

How to install [FORCA] Command Grid on a fresh server and bring it into production, using
either the one-shot provisioning script or the Makefile lifecycle directly.

## Table of contents

- [Two installation paths](#two-installation-paths)
- [Path A: the one-shot script](#path-a-the-one-shot-script)
- [Path B: the Makefile lifecycle](#path-b-the-makefile-lifecycle)
- [Data bootstrap](#data-bootstrap)
- [Secrets management](#secrets-management)
- [First-install checklist](#first-install-checklist)
- [Where to go next](#where-to-go-next)

## Two installation paths

| Path | Best for | Entry point |
|---|---|---|
| **A. Automated** | A dedicated fresh Ubuntu 24.04 LTS+ host you're happy to hand over entirely to this application | `sudo deploy/deploy-ubuntu-26.04.sh ...` |
| **B. Manual** | A host you already manage, or a non-Ubuntu Docker host, or you want an external reverse proxy | `make setup` → edit `.env` → `make deploy` |

Both paths converge on the same running stack (`docker-compose.prod.yml`) and the same
`Makefile` targets for everyday operation. See [Requirements](./requirements.md) first.

## Path A: the one-shot script

`deploy/deploy-ubuntu-26.04.sh` provisions a fresh Ubuntu host and deploys the
application end to end. It is **idempotent** — re-running it upgrades an existing
install (pulls, rebuilds, migrates) rather than failing or duplicating work.

```bash
sudo ./deploy/deploy-ubuntu-26.04.sh \
  --domain grid.example.com \
  --repo https://github.com/ebrandi/forca-command-grid.git \
  --branch main \
  --admin-email you@example.com \
  --contact-email you@example.com \
  --sso-client-id <eve_sso_client_id> \
  --sso-client-secret <eve_sso_client_secret> \
  --home-corp-id <corporation_id>
```

### Flags

| Flag | Required | Purpose |
|---|---|---|
| `--domain` | For TLS | Public FQDN that resolves to this server |
| `--repo` | Yes | Git URL of the application repository |
| `--branch` | No (default `main`) | Branch/tag to deploy |
| `--app-dir` | No (default `/opt/forca`) | Install directory |
| `--admin-email` | For TLS | Email for Let's Encrypt registration and the Django superuser |
| `--sso-client-id` / `--sso-client-secret` | Recommended | EVE SSO application credentials |
| `--contact-email` | Recommended | Embedded in the ESI `User-Agent` |
| `--home-corp-id` | Recommended | Home corporation's numeric EVE id |
| `--no-tls` | No | Skip certbot (IP/staging box; bring your own certificate) |
| `--skip-bootstrap` | No | Don't import SDE/images on this run |
| `--skip-app` | No | Provision the host only; don't build/run the application |
| `-h`, `--help` | No | Show usage |

### What it does, in order

1. Pre-flight checks (must run as root; `--repo` required; `--domain` + `--admin-email`
   required unless `--no-tls`).
2. Updates the OS and installs base packages (`ca-certificates`, `git`, `ufw`,
   `fail2ban`, `unattended-upgrades`, `openssl`, …).
3. Installs Docker Engine + the Compose plugin from Docker's official apt repository (if
   not already present).
4. Creates the non-root `forca` service user and `/opt/forca` (or your `--app-dir`).
5. Clones the repository (or fetches/checks out/fast-forwards an existing checkout).
6. **Generates `/opt/forca/app/.env` with freshly random secrets** — `DJANGO_SECRET_KEY`,
   `POSTGRES_PASSWORD`, `TOKEN_ENCRYPTION_KEY`, `REDIS_PASSWORD` — mode `600`, and
   **never overwrites an existing `.env`** on a re-run.
7. Configures `ufw` (deny all inbound by default; allow SSH, 80, 443 only), enables
   `fail2ban`, and configures `unattended-upgrades`.
8. Stamps the deployed Git commit (`deploy/stamp-version.sh`) so the application footer
   shows exactly what's running.
9. Builds the application image, starts Postgres and Redis, then applies migrations and
   `collectstatic` **on the new image while the app containers are not yet running**.
10. Starts the full stack, waits for it to become ready (`scripts/wait-for-services.sh`),
    and restarts nginx last. The order matters on a **re-run** against an existing install:
    starting new code before migrating runs it against the old schema, and a new column on a
    hot table would then fail every session-bearing request until the migration lands. See
    [Upgrades](./upgrades.md).
11. Runs the **full SDE + PI + referenced-image bootstrap**
    (`scripts/bootstrap-data.sh`), unless `--skip-bootstrap`.
12. Ensures a Django superuser exists for the given `--admin-email`.
13. Obtains a TLS certificate via **certbot standalone** and installs renewal hooks that
    manage the nginx container (`scripts/cert-init.sh`), unless `--no-tls`.
14. Installs a `forca.service` systemd unit (starts the stack on boot) and a nightly
    PostgreSQL backup cron job (`/etc/cron.daily/forca-db-backup`, 14-day retention).

### After the script finishes

1. Set the EVE application's `redirect_uri` to `https://<your-domain>/auth/eve/callback/`.
2. Confirm `EVE_SSO_CLIENT_ID` / `EVE_SSO_CLIENT_SECRET` and a real `ESI_USER_AGENT`
   contact in `.env`; apply changes with `docker compose -f docker-compose.prod.yml up -d`.
3. Log in, link a **Director** character, and authorize corp scopes for full data (see
   [Permissions and Roles](../permissions-and-roles.md#esi-scopes)).

## Path B: the Makefile lifecycle

Every day-to-day production operation goes through the `Makefile`, a thin wrapper over
`docker compose -f docker-compose.prod.yml` and the `scripts/` helpers. Run `make help`
to list every target.

```bash
git clone https://github.com/ebrandi/forca-command-grid.git
cd forca-command-grid

make setup                        # creates .env from .env.example (never overwrites)
$EDITOR .env                      # fill in secrets + EVE SSO (see configuration.md)

make deploy                       # build, migrate, then start the stack (in that order)
make bootstrap                    # full SDE + PI rulebook + referenced images
make create-admin EMAIL=you@example.com
make health                       # confirm everything is up
```

| Target | What it runs |
|---|---|
| `make setup` | `cp .env.example .env` if `.env` doesn't already exist |
| `make build` | `docker compose -f docker-compose.prod.yml build` |
| `make deploy` | Builds the image, migrates and collects static on it, and only then starts the stack — see [Upgrades](./upgrades.md) for why that order |
| `make update` | `scripts/update.sh` — the safe upgrade path (see [Upgrades](./upgrades.md)) |
| `make migrate` | Applies database migrations |
| `make collectstatic` | Collects static assets |
| `make bootstrap` | `scripts/bootstrap-data.sh` — full SDE + PI + referenced images |
| `make bootstrap-sample` | Tiny bundled sample SDE fixture (dev/CI only, not for production) |
| `make import-sde` | `import_sde_fuzzwork` directly |
| `make import-assets` | `mirror_type_images --referenced-only` directly |
| `make prices` | `price_types` — first-pass Jita pricing |
| `make create-admin EMAIL=...` | Ensures a Django superuser exists |
| `make health` | `scripts/healthcheck.sh` |
| `make logs` / `make ps` | Tail logs / show container status |
| `make restart` / `make down` | Restart the stack / stop it (volumes preserved) |
| `make shell` / `make dbshell` | Django shell / `psql` shell |
| `make backup` / `make restore FILE=...` | See [Backup and Restore](./backup-and-restore.md) |
| `make cert DOMAIN=... EMAIL=...` | Obtain/renew the TLS certificate (run with `sudo`) |
| `make config-check` | Validates that both compose files parse |

`make deploy` intentionally stops **before** loading reference data — run `make
bootstrap` afterward on a first install (the UI shows raw numeric IDs instead of names
until the SDE is loaded).

## Data bootstrap

`scripts/bootstrap-data.sh` (invoked by `make bootstrap`, and by the one-shot script
unless `--skip-bootstrap`) loads the EVE reference data a fresh instance needs. Every
stage is **idempotent** — safe to re-run; importers upsert and skip existing rows/files.

| Stage | Command | Purpose |
|---|---|---|
| 1. Static Data Export | `import_sde_fuzzwork` (or `load_sde` with `--sample`) | Type/system/region/skill names — **required** for a working UI |
| 2. Planetary Industry rulebook | `load_pi_static` | PI materials/schematics |
| 3. Type images | `mirror_type_images --referenced-only` (default), `--all-images`, or skipped with `--no-images` | Mirrors icons/renders seen on kills and doctrines; anything unmirrored falls back to nginx's CCP proxy-cache |
| 4. Prices (optional) | `price_types`, only with `--with-prices` | First-pass Jita pricing so ISK values show immediately; the daily beat job refreshes them thereafter |

```bash
scripts/bootstrap-data.sh                          # default: full SDE, referenced images
scripts/bootstrap-data.sh --sample --no-images      # dev/CI only — tiny fixture, no images
scripts/bootstrap-data.sh --all-images --with-prices
```

## Secrets management

- `.env` lives at the repository root, is **git-ignored**, and must be kept at
  **mode 600**, owned by the application user. The one-shot script sets this
  automatically; on the manual path, set it yourself after editing the file.
- The one-shot script generates `DJANGO_SECRET_KEY`, `POSTGRES_PASSWORD`,
  `TOKEN_ENCRYPTION_KEY`, and `REDIS_PASSWORD` with `openssl rand`, and never
  overwrites an existing `.env`.
- **`TOKEN_ENCRYPTION_KEY` encrypts stored OAuth refresh tokens and integration
  credentials at rest.** Losing it makes those tokens unrecoverable — members simply
  re-authorize, but any configured integration credentials would need to be re-entered.
  **Back this key up separately from your database backups.** See
  [Backup and Restore](./backup-and-restore.md).
- Scripts in this repository never print secret values to the console or logs.

## First-install checklist

1. Confirm [Requirements](./requirements.md) are met (OS, DNS, EVE application
   registered).
2. Choose Path A or Path B above and run it.
3. `make health` — expect all containers healthy, `/healthz` returning 200, no
   unapplied migrations, Redis `PONG`, Celery worker responding.
4. If you deployed with `--skip-bootstrap` or via the manual path without `make
   bootstrap`, run `make bootstrap` now — the UI shows raw numeric IDs until the SDE is
   loaded.
5. Open `https://<your-domain>/` and confirm the landing page renders.
6. Log in with EVE SSO.
7. Link a **Director** character and authorize corporation scopes — this unlocks
   members, wallets, structures, and killmails (see
   [Permissions and Roles](../permissions-and-roles.md#esi-scopes)).
8. Confirm a TLS certificate is installed and auto-renewing (`sudo certbot renew
   --dry-run` if you used certbot).
9. `make backup` once, to confirm the backup path works, and store
   `TOKEN_ENCRYPTION_KEY` somewhere safe outside the host.
10. Review [Security Hardening](./security-hardening.md).

## Where to go next

- [Configuration](./configuration.md) — walk through required and optional settings.
- [Operations Runbook](./operations-runbook.md) — day-to-day checklists once you're live.
- [Troubleshooting](./troubleshooting.md) — if any step above didn't go cleanly.
