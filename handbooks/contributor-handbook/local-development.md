# Local Development

## Table of contents

- [Prerequisites](#prerequisites)
- [Quick start](#quick-start)
- [What `make dev` starts](#what-make-dev-starts)
- [Loading data](#loading-data)
- [Settings modules](#settings-modules)
- [Ports and host binding](#ports-and-host-binding)
- [Running against real EVE SSO / ESI locally](#running-against-real-eve-sso--esi-locally)
- [Everyday commands](#everyday-commands)

## Prerequisites

- Docker Engine with the Compose plugin (the `docker compose` v2 CLI; the `Makefile`
  falls back to legacy `docker-compose` if v2 isn't found).
- Nothing else — Python, PostgreSQL, and Redis all run in containers. The project
  intentionally keeps the host clean.

## Quick start

```bash
make dev                 # build + start web, worker, beat, postgres, redis (autoreload)
make bootstrap-sample    # load the tiny bundled sample SDE fixture (dev/CI only)
docker compose exec web python manage.py seed_demo   # roles, home corp, demo doctrine
docker compose exec web python manage.py createsuperuser
```

Then open **http://127.0.0.1:8000/**. The role-gated admin console lives at `/ops/`
once you hold the `officer` role or higher (see
[permissions-and-roles.md](../permissions-and-roles.md)).

## What `make dev` starts

`make dev` runs `docker compose -f docker-compose.yml up -d --build`, which brings up
five services (see `docker-compose.yml`):

| Service | Command | Notes |
|---|---|---|
| `web` | `migrate --noinput && runserver 0.0.0.0:8000` | Django's autoreloading dev server. |
| `worker` | `celery -A config worker -l info --concurrency 2` | Executes Celery tasks. |
| `beat` | `celery -A config beat -l info` | Fires the periodic schedule. |
| `postgres` | `postgres:16-alpine` | Database `forca` / user `forca` / password `forca` (dev only). |
| `redis` | `redis:7-alpine` | Cache + Celery broker. |

The dev compose file sets `DJANGO_SETTINGS_MODULE=config.settings.dev`,
`DJANGO_DEBUG=1`, and mounts the repository into `/app` so code edits are picked up
immediately by the autoreloading `runserver` — no image rebuild needed for Python or
template changes (only for dependency changes, which do need `--build`).

## Loading data

- `make bootstrap-sample` — the small, bundled sample Static Data Export (SDE) plus PI
  data, **no images**. This is what dev and CI use; it is fast and requires no network
  access to Fuzzwork/EVE Ref.
- `make bootstrap` — the **full** SDE, PI data, and referenced EVE type images. This is
  the production/first-install path (`scripts/bootstrap-data.sh`) and is much heavier;
  avoid it for routine local development.
- `python manage.py seed_demo` — creates the RBAC roles/permissions, a demo home
  corporation, and a demonstration doctrine so the UI has something to show
  immediately after a fresh database.
- `python manage.py createsuperuser` — a Django superuser bypasses `core.rbac` role
  checks entirely (`effective_rank` treats `is_superuser` as the top `admin` rank), so
  it's the fastest way to explore every gated page locally without wiring up EVE SSO.

## Settings modules

Settings live under `config/settings/` as one module per environment, all importing
from a shared `base.py`:

| Module | Used by | Key differences from `base.py` |
|---|---|---|
| `config.settings.base` | Never used directly | Shared defaults: installed apps, middleware, templates, database, cache, Celery, DRF/OpenAPI, ESI/SSO, feature-provider settings, logging. |
| `config.settings.dev` | `docker-compose.yml` (`DJANGO_SETTINGS_MODULE`) | `DEBUG=True`; concrete `ALLOWED_HOSTS` (`localhost`, `127.0.0.1`, `[::1]`, `web`); allows `TOKEN_ENCRYPTION_KEY` to be derived from `SECRET_KEY` via the explicit `ALLOW_DERIVED_TOKEN_KEY = True` opt-in (never implied by `DEBUG` alone). |
| `config.settings.test` | `pytest` (`pyproject.toml`'s `DJANGO_SETTINGS_MODULE`) | `CELERY_TASK_ALWAYS_EAGER=True`; MD5 password hasher for speed; local-memory cache (no Redis dependency); a deterministic Fernet key; known SSO test client id/secret; `FORCA_HOME_CORP_ID=98000001`. |
| `config.settings.prod` | `docker-compose.prod.yml` | `DEBUG=False`; requires `DJANGO_SECRET_KEY`/`TOKEN_ENCRYPTION_KEY`/`DJANGO_ALLOWED_HOSTS` (raises `ImproperlyConfigured` if missing or left at the insecure default); HTTPS redirect, HSTS, secure cookies; `CONN_MAX_AGE`/`CONN_HEALTH_CHECKS`; hashed/compressed static storage; the stock Django admin is unmounted by default. |

`manage.py` defaults `DJANGO_SETTINGS_MODULE` to `config.settings.dev` if the
environment variable isn't already set. See
[configuration-reference.md](../configuration-reference.md) for the full list of
environment variables each module reads.

## Ports and host binding

Every port published by the dev compose file is bound to `127.0.0.1` only (never
`0.0.0.0`), matching the same discipline used in production:

```yaml
web:
  ports: ["127.0.0.1:8000:8000"]
postgres:
  ports: ["127.0.0.1:5432:5432"]
redis:
  ports: ["127.0.0.1:6379:6379"]
```

This keeps a `DEBUG=True` development instance from ever becoming reachable on a
routable network interface, even on a shared or cloud development box.

## Running against real EVE SSO / ESI locally

By default, dev has no SSO client configured (`EVE_SSO_CLIENT_ID`/`SECRET` default to
empty strings in `base.py`), so member login via EVE SSO won't work until you register
an application:

1. Register an application at CCP's developer portal with a callback URL of
   `http://localhost:8000/auth/eve/callback/` (matches `EVE_SSO_CALLBACK_URL`'s
   default) and the scopes listed in `EVE_SSO_DEFAULT_SCOPES`
   (`config/settings/base.py`) at minimum.
2. Set `DJANGO_EVE_SSO_CLIENT_ID` / `DJANGO_EVE_SSO_CLIENT_SECRET`-equivalent
   variables — concretely, `EVE_SSO_CLIENT_ID` and `EVE_SSO_CLIENT_SECRET` — in a root
   `.env` file (loaded automatically by `config/settings/base.py` if present; git-ignored).
3. Set `FORCA_HOME_CORP_ID` to your corporation's real EVE corporation id so
   `apps.sso.services.sync_roles_for_user` can recognise your character as a member
   (and, if your character holds the in-game Director role, auto-grant the app's
   `director` role at login).
4. Restart `web` so the new environment variables are picked up.

All ESI calls remain confined to Celery tasks (see
[architecture.md](./architecture.md#the-golden-rule-esi-and-llm-calls-only-from-celery-workers)),
so the `worker` service must also be running (it is, under `make dev`) for
syncs to actually populate data after you log in. `ESI_BASE_URL` defaults to CCP's
real `https://esi.evetech.net` in every settings module — there is no local ESI mock
server in this repository; `responses`-mocked HTTP is used only inside the test suite
(see [testing.md](./testing.md)).

## Everyday commands

```bash
docker compose logs -f web             # tail the dev web service
docker compose exec web python manage.py shell
docker compose exec web python manage.py dbshell
make dev-logs                          # tail all dev services
make dev-down                          # stop the dev stack (volumes preserved)
```

For running the test suite and linter, see [testing.md](./testing.md).
