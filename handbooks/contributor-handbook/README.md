# Contributor Handbook

[FORCA] Command Grid is a Django 5.2 + Celery + PostgreSQL 16 + Redis 7 operations hub
for an EVE Online corporation: a killboard, doctrine library, industry and market
tools, readiness scoring, fleet operations, member services, and a strategic
intelligence layer, all served from one Django project.

This handbook is for **developers, architects, testers, and security reviewers** who
want to understand the codebase well enough to change it safely. If you are deploying
or operating an instance rather than modifying the source, see the
[Operator Handbook](../operator-handbook/README.md) and
[Administrator Handbook](../administrator-handbook/README.md) instead.

## Table of contents

- [Who this is for](#who-this-is-for)
- [Technology stack](#technology-stack)
- [Repository structure](#repository-structure)
- [Where to go next](#where-to-go-next)

## Who this is for

- **Developers** fixing bugs, adding features, or reviewing pull requests.
- **Architects** evaluating the system design before proposing a change.
- **Testers** writing or extending automated coverage.
- **Security reviewers** auditing the authentication, authorization, and outbound
  integration surfaces.

Start with [CONTRIBUTING.md](../../CONTRIBUTING.md) in the repository root for the
practical workflow (branching, checklist, how to open a pull request), and use this
handbook for the depth behind each step.

## Technology stack

| Layer | Technology | Notes |
|---|---|---|
| Language / runtime | Python 3.12 | Pinned in `pyproject.toml` (`requires-python = ">=3.12"`) and the `python:3.12-slim` base image. |
| Web framework | Django 5.2 | `Django>=5.2.15,<5.3`. |
| Background jobs | Celery 5.5 | Redis broker + result backend; beat schedule in `config/celery.py`. |
| Database | PostgreSQL 16 | `postgres:16-alpine` in `docker-compose.yml`; driver is `psycopg[binary]` 3.x. |
| Cache / broker | Redis 7 | `redis:7-alpine`; also the Celery broker and Django cache backend. |
| API tooling | Django REST Framework + drf-spectacular | Configured (session auth, `IsAuthenticated` default, `PageNumberPagination`, page size 50) for the app's JSON surfaces; see [api-reference.md](./api-reference.md). |
| WSGI/ASGI server | gunicorn | `config.wsgi:application`, 3 workers by default in the image `CMD`. |
| Static files | WhiteNoise | Compressed (dev/test) / compressed-manifest (prod) storage; no external CDN. |
| Front end | Server-rendered Django templates + htmx + Alpine.js + Tailwind CSS + Chart.js + svg-pan-zoom | All vendored under `static/`, no CDN scripts (CSP hardening). See [architecture.md](./architecture.md). |
| Secrets / crypto | `cryptography` (Fernet) + `PyJWT` | OAuth refresh-token/credential encryption at rest and EVE SSO JWT validation. |
| XML parsing | `defusedxml` | Hardened parser for EVE-client fitting XML import. |
| Lint/format | ruff | Config in `pyproject.toml`: line length 120, `py312`, rule sets `E,F,I,UP,B,DJ,S`. |
| Tests | pytest + pytest-django + pytest-cov + `responses` | `factory-boy` is present as a dev dependency; the current suite builds fixtures with plain Django ORM calls (see `tests/conftest.py`) rather than factory classes. |
| Containers | Docker + Docker Compose | `docker-compose.yml` (dev) and `docker-compose.prod.yml` (production); one application image shared by the web, worker, and beat services. |

## Repository structure

```
forca-command-grid/
├── apps/            # One Django app per bounded context (killboard, sso, readiness, …)
├── core/            # Shared primitives: RBAC, feature flags, middleware, ESI client, audit
├── config/          # Settings modules, Celery app + beat schedule, root URLconf, WSGI/ASGI
├── templates/        # Server-rendered Django templates, one directory per app namespace
├── static/           # Compiled CSS, hand-written JS, and vendored front-end libraries
├── frontend/         # Node-based build that produces the files under static/ (no runtime Node)
├── deploy/           # Production deployment assets (nginx, compose overlays, certs)
├── scripts/          # Operational shell scripts (bootstrap, backup, health checks, deploy helpers)
├── tests/            # Cross-app pytest suite (~270 test modules) plus tests/conftest.py fixtures
├── docs/             # Design docs, ADRs, and audit records from the project's build history
└── handbooks/        # This documentation set (contributor, operator, administrator, end-user)
```

Each app under `apps/` owns one bounded context — its models, views, templates
namespace, Celery tasks, and (where applicable) admin console pages. `core/` holds
primitives every app depends on (`core.rbac`, `core.features`, `core.middleware`,
`core.esi`, `core.audit`, `core.mixins`, `core.freshness`, `core.version`) rather than
duplicating them per app. See [architecture.md](./architecture.md) for how these
pieces fit together at request time, and [domain-model.md](./domain-model.md) for a
one-line summary of every app's responsibility.

## Where to go next

- [architecture.md](./architecture.md) — request flow, middleware stack, web/worker/beat
  topology, the ESI-only-from-workers rule, and the front-end approach.
- [local-development.md](./local-development.md) — Docker dev setup, seeding data, running
  against real EVE SSO/ESI.
- [testing.md](./testing.md) — running pytest, mocking ESI, coverage, ruff.
- [domain-model.md](./domain-model.md) — RBAC/SSO/SDE model overview and an ER diagram of
  the core identity relationships.
- [api-reference.md](./api-reference.md) — the HTML-view-first routing pattern, JSON
  endpoints, and where the OpenAPI schema is generated from.
- [esi-integration.md](./esi-integration.md) — OAuth2/PKCE, token encryption, the
  disciplined ESI client, scopes, and how to add a new ESI integration.
- [background-jobs.md](./background-jobs.md) — the Celery architecture and how to add a
  scheduled task (full current schedule: [../reference/background-jobs.md](../reference/background-jobs.md)).
- [security-guidelines.md](./security-guidelines.md) — security principles for
  contributors and how to add a new admin setting safely.
- [pull-request-guide.md](./pull-request-guide.md) — branching, review process,
  checklist, coding conventions, and the release process.

Also see, at the repository root:

- [CONTRIBUTING.md](../../CONTRIBUTING.md) — the practical contribution workflow.
- [SECURITY.md](../../SECURITY.md) — vulnerability reporting and the security posture summary.

And in `handbooks/`, alongside this handbook:

- [feature-catalog.md](../feature-catalog.md) — every member-facing feature, grouped by area.
- [permissions-and-roles.md](../permissions-and-roles.md) — role tiers, capabilities, and
  ESI scopes in full detail.
- [configuration-reference.md](../configuration-reference.md) — every environment variable
  and database-stored setting.
