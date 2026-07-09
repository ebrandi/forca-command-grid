# Dependency Inventory

A structured inventory of third-party dependencies and external services used by
[FORCA] Command Grid, for maintainers and reviewers. Licences are best-effort from public
package metadata; see [licence-review.md](./licence-review.md) for the review notes and
[`NOTICE.md`](../../NOTICE.md) for the acknowledgements file.

Columns: **Ecosystem** (where it comes from), **Type** (runtime / dev-test / build / asset /
deployment / service), **Licence** (best effort).

## Table of contents

- [Python runtime](#python-runtime)
- [Python development and test](#python-development-and-test)
- [Frontend](#frontend)
- [Container images](#container-images)
- [External services and data](#external-services-and-data)

## Python runtime

Source: [`requirements.txt`](../../requirements.txt) (PyPI).

| Package | Type | Licence | Purpose |
|---|---|---|---|
| Django | runtime | BSD-3-Clause | Web framework, ORM, migrations, templating |
| djangorestframework | runtime | BSD-3-Clause | REST API layer |
| drf-spectacular | runtime | BSD-3-Clause | OpenAPI schema generation |
| celery | runtime | BSD-3-Clause | Background tasks + scheduler |
| redis | runtime | MIT | Redis client (cache + broker) |
| psycopg[binary] | runtime | LGPL-3.0-or-later | PostgreSQL driver |
| django-environ | runtime | MIT | Env-var configuration |
| gunicorn | runtime | MIT | WSGI application server |
| whitenoise | runtime | MIT | Static file serving |
| defusedxml | runtime | PSF-2.0 | Hardened XML parsing (fitting import) |
| cryptography | runtime | Apache-2.0 OR BSD-3-Clause | Fernet token/credential encryption |
| PyJWT | runtime | MIT | JWT validation |
| requests | runtime | Apache-2.0 | HTTP client (ESI, integrations) |
| urllib3 | runtime | MIT | HTTP transport (pinned) |
| certifi | runtime | MPL-2.0 | Root CA bundle (pinned) |
| pip-audit | runtime | Apache-2.0 | Dependency vulnerability scanning |

## Python development and test

Source: [`requirements-dev.txt`](../../requirements-dev.txt) (PyPI).

| Package | Type | Licence | Purpose |
|---|---|---|---|
| pytest | dev-test | MIT | Test runner |
| pytest-django | dev-test | BSD-3-Clause | Django test integration |
| pytest-cov | dev-test | MIT | Coverage |
| factory-boy | dev-test | MIT | Test data factories |
| responses | dev-test | Apache-2.0 | HTTP mocking |
| ruff | dev-test | MIT | Lint + format |

## Frontend

Source: [`frontend/package.json`](../../frontend/package.json) (npm). Runtime libraries are
vendored into `static/js/vendor/` at build time — no CDN scripts are served.

| Package | Type | Licence | Purpose |
|---|---|---|---|
| alpinejs | runtime (vendored) | MIT | Interactive UI behaviour |
| chart.js | runtime (vendored) | MIT | Charts and graphs |
| htmx.org | runtime (vendored) | BSD-2-Clause / MIT (verify) | HTML partial updates |
| svg-pan-zoom | runtime (vendored) | BSD-2-Clause | Map pan/zoom |
| tailwindcss | build | MIT | Stylesheet compilation |

## Container images

Source: [`Dockerfile`](../../Dockerfile), [`docker-compose.prod.yml`](../../docker-compose.prod.yml).

| Image | Type | Licence / terms | Purpose |
|---|---|---|---|
| `python:3.12-slim` | deployment | PSF + Debian | Application runtime |
| `nginx:1.27-alpine` | deployment | BSD-2-Clause + Alpine | TLS, proxy, image cache |
| `postgres:16-alpine` | deployment | PostgreSQL License + Alpine | Database |
| `redis:7-alpine` | deployment | BSD-3-Clause + Alpine | Cache + broker |

## External services and data

| Service | Type | Terms | Purpose |
|---|---|---|---|
| EVE ESI (CCP hf.) | service | CCP property; developer registration required | Game data |
| EVE SSO (CCP hf.) | service | CCP property | Authentication |
| EVE SDE (CCP hf.) | data | CCP property | Reference data |
| EVE Image Service (CCP hf.) | service | CCP property | Imagery |
| Fuzzwork | service | Community | SDE conversions + prices |
| EveRef | service | Community | Reference/killmail/history backfill |
| zKillboard | service | Community | Killmail feed |
| MiniMax | service (optional) | Provider terms | LLM for Command Intelligence |
| Discord / Slack / Telegram / WhatsApp | service (optional) | Provider terms | Outbound alerting / role sync |

## Regenerating this inventory

- Python: `pip show <package>` for metadata, and
  `docker compose -f docker-compose.prod.yml exec web python manage.py audit_dependencies`
  (or `pip-audit -r requirements.txt`) for vulnerabilities.
- Frontend: inspect `frontend/package-lock.json` for exact pinned versions.
