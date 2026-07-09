# Deployment — [FORCA] Command Grid

Provisioning + operations for a **fresh Ubuntu server** (24.04 LTS or newer) running the
**fully containerized** stack with Docker Compose. Everything — including nginx (TLS on 80/443)
— runs in containers; there is **no host nginx**. This matches
[`docker-compose.prod.yml`](../docker-compose.prod.yml).

> **New here? Start with the Operator Handbook:**
> [requirements](../handbooks/operator-handbook/requirements.md) ·
> [deployment](../handbooks/operator-handbook/deployment.md) ·
> [configuration](../handbooks/operator-handbook/configuration.md) ·
> [operations runbook](../handbooks/operator-handbook/operations-runbook.md) ·
> [upgrades](../handbooks/operator-handbook/upgrades.md) ·
> [backup & restore](../handbooks/operator-handbook/backup-and-restore.md) ·
> [troubleshooting](../handbooks/operator-handbook/troubleshooting.md)

## Contents

| File | Purpose |
|---|---|
| `deploy-ubuntu-26.04.sh` | One-shot, idempotent host provisioning + deploy (run as root). |
| `stamp-version.sh` | Materialise the deployed commit hash into `.git-commit` (footer build marker). |
| `verify-prod.sh` | Post-deploy sanity checks against a running stack. |
| `nginx/forca.prod.conf` | The nginx **container** config (mounted into the nginx service). |
| `nginx/eveimg-placeholder.svg` | "N/A" image for types CCP has no art for. |

The canonical env template is [`.env.example`](../.env.example) at the repo root; operator
commands live in the [`Makefile`](../Makefile) and [`scripts/`](../scripts).

## Prerequisites

- A fresh **Ubuntu 24.04 LTS+** server with root/sudo.
- A **domain** with an A/AAAA record at the server (for Let's Encrypt TLS; use `--no-tls` for an
  IP/staging box and bring your own cert).
- The **application git URL**.
- A registered **EVE application** (developers.eveonline.com) with client id/secret and a
  redirect of `https://<your-domain>/auth/eve/callback/`.

## Usage

```bash
sudo ./deploy-ubuntu-26.04.sh \
  --domain grid.example.com \
  --repo https://github.com/ebrandi/forca-command-grid.git \
  --branch main \
  --admin-email you@example.com \
  --contact-email you@example.com \
  --sso-client-id <eve_sso_client_id> \
  --sso-client-secret <eve_sso_client_secret> \
  --home-corp-id <corporation_id>
```

The script updates the OS; installs Docker + `ufw`/`fail2ban`/unattended-upgrades; creates the
non-root `forca` user; clones the repo; generates `/opt/forca/app/.env` with strong random
secrets (mode 600, never overwritten); configures the firewall (22/80/443); builds and starts
the containerized stack; runs `migrate` + `collectstatic` + the **full SDE** bootstrap; ensures
a superuser; obtains TLS via **certbot standalone** with container-managing renewal hooks
(`../scripts/cert-init.sh`); and installs a boot `systemd` unit + nightly DB backup cron.

Flags: `--no-tls`, `--skip-bootstrap`, `--skip-app`, `--help`. Full walkthrough:
[the deployment guide](../handbooks/operator-handbook/deployment.md).

## Upgrades

Re-run the same command (idempotent), or from the app directory:

```bash
make update      # backup → pull --ff-only → stamp → rebuild → migrate → health
```

See [upgrades.md](../handbooks/operator-handbook/upgrades.md).

## TLS model

nginx runs as a **container** and reads its cert from `<app>/certs/{forca.crt,forca.key}`.
certbot runs in **standalone** mode; renewal hooks stop the nginx container to free port 80,
copy the renewed cert into `certs/`, and restart it (`certbot.timer` automates renewal). For an
external LB / Cloudflare, skip certbot and drop your cert into `certs/`.

## Security notes

- Secrets live only in `/opt/forca/app/.env` (mode 600, owned by `forca`); scripts never print
  them.
- The `TOKEN_ENCRYPTION_KEY` encrypts stored OAuth refresh tokens; **back it up** — losing it
  means members re-authorise. See [backup-and-restore.md](../handbooks/operator-handbook/backup-and-restore.md).
- Only 22/80/443 are open; Postgres/Redis have no host port mapping. Containers run non-root
  with `cap_drop: ALL` + `no-new-privileges`.
- After deploy, set the EVE app `redirect_uri` to your domain and authorise a **Director** token
  for full corp data (see [ESI integration](../handbooks/contributor-handbook/esi-integration.md)).
