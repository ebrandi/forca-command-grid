# Requirements

What a host needs before you deploy [FORCA] Command Grid, how to size it, and what
external accounts you must register first.

## Table of contents

- [Operating system](#operating-system)
- [Software prerequisites](#software-prerequisites)
- [Hardware sizing](#hardware-sizing)
- [Required external services](#required-external-services)
- [Network and firewall](#network-and-firewall)
- [EVE SSO / ESI application registration](#eve-sso--esi-application-registration)
- [DNS](#dns)

## Operating system

| Requirement | Detail |
|---|---|
| OS | **Ubuntu 24.04 LTS or newer** (the provisioning script targets Ubuntu; other Docker-capable Linux hosts work with the manual path) |
| Access | Root or `sudo` for the automated install; a Docker-capable user for the manual path |
| Architecture | x86_64 (matches the published base images: `python:3.12-slim`, `postgres:16-alpine`, `redis:7-alpine`, `nginx:1.27-alpine`) |

## Software prerequisites

Everything runs in containers, so the host itself needs very little:

| Requirement | Notes |
|---|---|
| Docker Engine + Compose plugin | `deploy/deploy-ubuntu-26.04.sh` installs this automatically from Docker's official apt repository. For the manual path, install per [docs.docker.com/engine/install](https://docs.docker.com/engine/install/). |
| `git` | To clone the repository. |
| `openssl` | To generate secrets (the deploy script uses it; you'll want it too for the manual path). |
| `ufw`, `fail2ban`, `unattended-upgrades` | Installed and configured by the deploy script; optional if you manage the host's firewall/patching yourself. |

No host-level Python, PostgreSQL, Redis, or nginx installation is required or expected —
installing them on the host would be redundant with (and could conflict with) the
containerized services.

## Hardware sizing

`docker-compose.prod.yml` bakes in tuning for a **reference host of 8 vCPU / 30 GB RAM**
(gunicorn 5 workers × 4 threads, Celery concurrency 8, PostgreSQL `shared_buffers=2GB` /
`effective_cache_size=12GB` / `max_connections=150`). Use that as your "recommended"
target; a smaller host works but with reduced concurrency headroom.

| Tier | CPU | RAM | Disk | Notes |
|---|---|---|---|---|
| **Minimum** | 2 vCPU | 4 GB | 20 GB SSD | Works for a small corp; reduce gunicorn/Celery concurrency and PostgreSQL memory settings in `docker-compose.prod.yml` to fit (see below) |
| **Recommended (reference host)** | 8 vCPU | 30 GB | 60+ GB SSD | Matches the tuning shipped in `docker-compose.prod.yml` as-is |

Per-container memory backstops (`mem_limit`, from `docker-compose.prod.yml`) — useful for
capacity planning even on a differently-sized host:

| Container | `mem_limit` | Rationale |
|---|---|---|
| `postgres` | 8g | Backstop above `shared_buffers` (2 GB) + `shm_size` (1 GB) + `work_mem` bursts |
| `worker` | 3g | SDE/EveRef imports are the heaviest jobs; ~600 MB steady |
| `web` | 2g | ~5 gthread workers ≈ 440 MB steady; recycled via `--max-requests` |
| `redis` | 768m | Backstop above Redis's own `--maxmemory 512mb` |
| `nginx` | 256m | Reverse proxy + image cache |
| `beat` | 512m | Scheduler only; ~100 MB steady |

If you deploy on a smaller host, lower `shared_buffers`/`effective_cache_size`/
`max_connections` in the `postgres` service command, and the gunicorn/Celery worker
counts in the `web`/`worker` commands, proportionally — and lower the matching
`mem_limit` values so the backstops still make sense for your host's total RAM. The host
has **no swap** by design in the reference tuning, so headroom matters; a small swap
file is a reasonable cushion on constrained hosts.

Disk grows with two things: the PostgreSQL volume (killmail/market history) and the
`eveimg_data` volume if you mirror the full image set (`--all-images`) rather than the
default referenced-only mirror.

## Required external services

| Service | Required? | Purpose |
|---|---|---|
| **PostgreSQL 16** | Yes | Primary datastore — runs as the `postgres` container; no external managed database is required or assumed |
| **Redis 7** | Yes | Django cache backend and Celery broker — runs as the `redis` container |
| **Celery (via Redis)** | Yes | Task queue for all background/ESI work — runs as the `worker` + `beat` containers |
| **nginx** | Yes | Reverse proxy and TLS terminator — runs as a container; there is no host nginx |
| **TLS certificate** | Yes (for a public HTTPS deployment) | Issued via **certbot in standalone mode** (`scripts/cert-init.sh`); bring-your-own-certificate is also supported |
| **DNS** | Yes (for TLS) | An A/AAAA record pointing at the host, required before requesting a Let's Encrypt certificate |
| **EVE SSO / ESI application** | Yes | Registered at [developers.eveonline.com](https://developers.eveonline.com); required for login and all game data |

All of the above run as containers defined in `docker-compose.prod.yml` except DNS and
the EVE application registration, which are external prerequisites you arrange yourself.

## Network and firewall

| Port | Direction | Purpose |
|---|---|---|
| 22 | Inbound | SSH |
| 80 | Inbound | HTTP → redirected to HTTPS by nginx; also used by certbot standalone during issuance/renewal |
| 443 | Inbound | HTTPS (application) |
| 5432 (PostgreSQL) | — | **Not exposed.** No host port mapping in `docker-compose.prod.yml`; reachable only inside the compose network |
| 6379 (Redis) | — | **Not exposed.** Same as above |

The automated deploy script configures `ufw` to `deny` all inbound traffic by default and
explicitly allow only SSH, 80, and 443. If you manage your own firewall, replicate that
policy — do not open PostgreSQL or Redis to the network.

## EVE SSO / ESI application registration

Before deploying, register an application at
[developers.eveonline.com](https://developers.eveonline.com):

| Field | Value |
|---|---|
| Callback URL | `https://<your-domain>/auth/eve/callback/` — must match `EVE_SSO_CALLBACK_URL` exactly |
| Scopes | Enable the baseline login scopes and any opt-in feature scopes you intend to use — see the full catalogue in [Permissions and Roles](../permissions-and-roles.md#esi-scopes) |
| Contact email | Not a field on the CCP application itself, but set a **real** contact address in `ESI_USER_AGENT` — CCP may throttle a generic or blank User-Agent |

You will need the application's **client ID** and **client secret** at deploy time
(`--sso-client-id` / `--sso-client-secret` for the automated script, or
`EVE_SSO_CLIENT_ID` / `EVE_SSO_CLIENT_SECRET` in `.env` for the manual path). See
[Configuration](./configuration.md) for how these values flow into the running
application.

A **second, optional** EVE application can be registered for read-only recruitment
candidate vetting (`RECRUITMENT_SSO_*`); see
[Third-Party Services](../third-party-services.md#eve-online-sso-and-esi).

## DNS

Point an **A (and/or AAAA) record** for your chosen domain at the host's public IP
address **before** requesting a TLS certificate — Let's Encrypt's standalone HTTP
challenge validates over port 80 using that domain. If you don't yet have a domain
pointed at the host, deploy with `--no-tls` and add TLS once DNS is ready.
