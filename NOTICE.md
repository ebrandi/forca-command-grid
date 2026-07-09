# Notices and Third-Party Acknowledgements

[FORCA] Command Grid is built on top of open-source software and integrates with
several external services and community data sources. This file acknowledges those
works and records their licences to the best of the maintainers' ability.

> **Scope and disclaimer.** The licence identifiers below are collected from public
> package metadata, project repositories, and distribution manifests. They are provided
> as a maintainer aid, **not as legal advice** and **not as a warranty of completeness**.
> Where a licence could not be confidently determined it is marked
> *maintainer review required*. Before distributing or relying on this project in a
> commercial or regulated context, verify each dependency's licence against its
> authoritative source. A machine-assisted, up-to-date inventory can be regenerated with
> `pip-audit` and `pip show`, and is summarised in
> [handbooks/reference/dependency-inventory.md](./handbooks/reference/dependency-inventory.md)
> and [handbooks/reference/licence-review.md](./handbooks/reference/licence-review.md).

## Project licence

[FORCA] Command Grid is released under the MIT License. See [`LICENSE`](./LICENSE).

## Python runtime dependencies

Declared in [`requirements.txt`](./requirements.txt).

| Package | Purpose in the project | Licence (best effort) |
|---|---|---|
| Django | Web framework, ORM, migrations, templating | BSD-3-Clause |
| djangorestframework | REST API layer | BSD-3-Clause |
| drf-spectacular | OpenAPI 3 schema generation for the API | BSD-3-Clause |
| celery | Background task queue and scheduler (Beat) | BSD-3-Clause |
| redis (redis-py) | Redis client — cache and Celery broker/result backend | MIT |
| psycopg[binary] | PostgreSQL database driver | LGPL-3.0-or-later |
| django-environ | Environment-variable configuration parsing | MIT |
| gunicorn | WSGI application server | MIT |
| whitenoise | Static file serving with compression/manifest | MIT |
| defusedxml | Hardened XML parsing for EVE-client fitting import | PSF-2.0 |
| cryptography | Fernet encryption of stored OAuth tokens and credentials | Apache-2.0 OR BSD-3-Clause |
| PyJWT | JWT validation for EVE SSO tokens | MIT |
| requests | HTTP client for ESI and outbound integrations | Apache-2.0 |
| urllib3 | HTTP transport (transitive, pinned for security) | MIT |
| certifi | Root CA bundle (transitive, pinned) | MPL-2.0 |
| pip-audit | Dependency vulnerability scanning (CI + scheduled job) | Apache-2.0 |

## Python development and test dependencies

Declared in [`requirements-dev.txt`](./requirements-dev.txt).

| Package | Purpose | Licence (best effort) |
|---|---|---|
| pytest | Test runner | MIT |
| pytest-django | Django integration for pytest | BSD-3-Clause |
| pytest-cov | Coverage reporting | MIT |
| factory-boy | Test data factories | MIT |
| responses | HTTP request mocking in tests | Apache-2.0 |
| ruff | Linter and formatter | MIT |

## Frontend libraries

The application serves **no third-party scripts from a CDN**. Runtime JavaScript
libraries are vendored into [`static/js/vendor/`](./static/js/vendor/) at build time
(see [`frontend/package.json`](./frontend/package.json)) so the Content-Security-Policy
can remain strict.

| Library | Purpose | Licence (best effort) |
|---|---|---|
| Alpine.js | Lightweight interactive UI behaviour | MIT |
| Chart.js | Charts and graphs | MIT |
| htmx | HTML-over-the-wire partial updates | BSD-2-Clause \* / MIT (see note) |
| svg-pan-zoom | Pan/zoom for region and jump maps | BSD-2-Clause |
| Tailwind CSS (build only) | Utility-first stylesheet compilation | MIT |

\* *maintainer review required:* confirm the current htmx licence for the pinned
version against its distribution before release.

## Container base images

Declared in [`Dockerfile`](./Dockerfile) and [`docker-compose.prod.yml`](./docker-compose.prod.yml).

| Image | Purpose | Licence / terms (best effort) |
|---|---|---|
| `python:3.12-slim` | Application runtime image | PSF License (Python) + Debian package licences |
| `nginx:1.27-alpine` | TLS termination, reverse proxy, image cache | BSD-2-Clause (nginx) + Alpine package licences |
| `postgres:16-alpine` | PostgreSQL database | PostgreSQL License + Alpine package licences |
| `redis:7-alpine` | Cache and Celery broker | BSD-3-Clause (Redis 7.x) + Alpine package licences |

## External services and community data sources

| Source | Use in the project | Notes |
|---|---|---|
| **EVE Swagger Interface (ESI)** — CCP hf. | Authenticated and public game-data reads | Requires a registered EVE application. Property of CCP hf. |
| **EVE Single Sign-On (SSO)** — CCP hf. | OAuth2 login and character authorisation | Property of CCP hf. |
| **EVE Static Data Export (SDE)** — CCP hf. | Type/system/region/skill reference data | Property of CCP hf. Imported via community conversions. |
| **EVE Image Service** (`images.evetech.net`) — CCP hf. | Ship renders, type icons, portraits, logos | Property of CCP hf. Proxied/mirrored at the edge. |
| **Fuzzwork** | SDE conversions and Jita price data | Community service. |
| **EveRef** | Optional reference-data / killmail / market-history backfill | Community service. |
| **zKillboard** | Corp killmail feed ingestion | Community service. |
| **MiniMax** (optional) | LLM provider for Command Intelligence features | Disabled unless configured. |
| **Discord / Slack / Telegram / WhatsApp (Meta or Twilio)** (optional) | Outbound alerting and role sync | Disabled unless configured. |
| **Google Fonts** (optional, referenced in style policy) | Web fonts | Referenced only; self-host to avoid the third-party origin if desired. |

## EVE Online / CCP Games

EVE Online and the EVE logo are the registered trademarks of CCP hf. All rights
reserved worldwide. All artwork, screenshots, characters, world facts, and other
recognizable features of the intellectual property relating to EVE Online are the
property of CCP hf. This project is a non-commercial fan project and is **not**
affiliated with, sponsored by, or endorsed by CCP hf. See
[handbooks/third-party-services.md](./handbooks/third-party-services.md) and the
trademark notice in [`README.md`](./README.md).

## AI-assisted development acknowledgement

[FORCA] Command Grid development was assisted by AI coding tools, including Claude Code
using Anthropic models such as Opus and Sonnet, and OpenCode using models including GLM,
MiniMax, Qwen, and Kimi. All code, documentation, architecture, security, and release
decisions remain the responsibility of the project maintainers.
