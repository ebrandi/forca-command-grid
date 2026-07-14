# API and Endpoints Reference

[FORCA] Command Grid is primarily a **server-rendered Django application**: most endpoints
return HTML pages (with htmx partials and JSON autocomplete helpers), not a public REST API.
This page maps the URL namespaces and describes the API surface. For the architecture, see
[contributor-handbook/api-reference.md](../contributor-handbook/api-reference.md).

## Table of contents

- [Endpoint model](#endpoint-model)
- [URL namespace map](#url-namespace-map)
- [System endpoints](#system-endpoints)
- [Authentication](#authentication)
- [OpenAPI schema](#openapi-schema)

## Endpoint model

- **HTML views** are the majority. They are organised per app under a URL prefix and
  namespace, and gated by roles, feature flags, and audiences (see
  [permissions-and-roles.md](../permissions-and-roles.md)).
- **htmx partial endpoints** return HTML fragments for in-page updates.
- **JSON helper endpoints** back autocompletes and pickers (for example type/system/hull
  search). They return JSON but are session-authenticated app endpoints, not a documented
  public API.
- **Django REST Framework** is configured as the established pattern for API-style views
  (session authentication, `IsAuthenticated` default, page-number pagination with a page
  size of 50), with **drf-spectacular** wired in as the schema generator. The application's
  endpoints are HTML views and session-authenticated JSON helpers rather than DRF
  `APIView`/`ViewSet` routes.

Access to any endpoint is enforced centrally by middleware and per-view decorators. There
is no token/API-key surface for third parties; authentication is via the user's session
after EVE SSO login.

## URL namespace map

Root URL configuration: [`config/urls.py`](../../config/urls.py). Each entry mounts an app's
routes under a prefix; the app's `urls.py` defines the named routes within it.

| Prefix | App (namespace) | Area |
|---|---|---|
| `/` | `identity` | Command Center dashboard, character pages, privacy/data rights |
| `/auth/eve/` | `sso` | EVE SSO login, callback, logout, ESI scopes, disconnect |
| `/i18n/` | `core.i18n` (no namespace) | Language selector target and the JavaScript message catalogue |
| `/killboard/` | `killboard` | Killboard, rankings, stats, intel, battle reports |
| `/doctrines/` | `doctrines` | Doctrine library, readiness, Shipyard |
| `/industry/` | `industry` | Industry Center |
| `/industry/pi/` | `planetary` | Planetary Industry guide and planner |
| `/skills/` | `skills` | Skill plans and gap analysis |
| `/stockpile/` | `stockpile` | Stockpile, assets, hauling board |
| `/market/` | `market` | Market dashboard and locations |
| `/onboarding/` | `onboarding` | New-player onboarding and glossary |
| `/mentorship/` | `mentorship` | Mentorship programme |
| `/recommendations/` | `recommendations` | Officer recommendations and relays |
| `/pingboard/` | `pingboard` | Alerting dashboard, composer, calendar, DM linking |
| `/ops/` | `admin_audit` | Admin console, audit log, integration health |
| `/pilots/` | `pilots` | Contribution ledger, Hall of Fame |
| `/tasks/` | `tasks` | Task board |
| `/srp/` | `srp` | Ship replacement programme |
| `/raffle/` | `raffle` | Raffle contests |
| `/readiness/` | `readiness` | Readiness platform |
| `/operations/` | `operations` | Fleet operations, timers, sovereignty |
| `/mining/` | `mining` | Mining ledger and payouts |
| `/erp/` | `erp` | Build jobs (redirects to the Industry Center job tracker by default) |
| `/kb/` | `kb` | Knowledge base |
| `/recruitment/` | `recruitment` | Recruitment desk and candidate OAuth |
| `/roster/` | `corporation` | Roster, finance, standings, structures |
| `/freight/` | `logistics` | Freight service |
| `/buyback/` | `buyback` | Buyback and appraisal |
| `/store/` | `store` | Corp Store |
| `/tools/` | `navigation` | Route/jump/range planners and maps |
| `/command/` | `command_intel` | Command Intelligence |
| `/comms/` | `comms_access` | Discord account linking |

The stock Django admin (`/admin/`) is **disabled by default in production** and mounted only
when `DJANGO_ENABLE_ADMIN=1`.

## System endpoints

| Path | Method | Purpose |
|---|---|---|
| `/healthz` | GET | Liveness/readiness probe. Returns JSON `{status, database}`; `200` when the database is reachable, `503` otherwise. Exempt from the HTTPS redirect for container health checks. |
| `/robots.txt` | GET | Served by nginx; disallows query-string (faceted) crawl paths. |
| `/eveimg/...` | GET | Same-origin EVE image proxy/cache (served by nginx). |
| `/i18n/setlang/` | POST | Language selector target; POST-only (`@require_POST`, [`core/i18n/views.py`](../../core/i18n/views.py)). Resolves the posted code against the enabled allow-list, sets the `forca_language` cookie, and for a signed-in operator also saves the account preference (`identity.User.language`). Redirects only to a same-origin `next`. |
| `/i18n/jsi18n/` | GET | Django's `JavaScriptCatalog`: the JS message catalogue, served as an external response (no inline script). |

## Authentication

All non-public endpoints require an authenticated session established via EVE SSO
(OAuth2 authorization-code + PKCE). See
[contributor-handbook/esi-integration.md](../contributor-handbook/esi-integration.md) and
[permissions-and-roles.md](../permissions-and-roles.md).

## OpenAPI schema

drf-spectacular is configured (`SPECTACULAR_SETTINGS` in
[`config/settings/base.py`](../../config/settings/base.py)) as the OpenAPI 3 schema
generator for DRF-backed views. The application is served as HTML views and
session-authenticated JSON helpers, which are not part of an OpenAPI surface; a
schema/swagger URL is mounted when a DRF-backed endpoint has a concrete consumer. See
[contributor-handbook/api-reference.md](../contributor-handbook/api-reference.md) for the
current state.
