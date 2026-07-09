# API Reference

## Table of contents

- [What kind of API this is](#what-kind-of-api-this-is)
- [URL namespace map](#url-namespace-map)
- [HTML views (the primary surface)](#html-views-the-primary-surface)
- [JSON and htmx partial endpoints](#json-and-htmx-partial-endpoints)
- [DRF and OpenAPI schema configuration](#drf-and-openapi-schema-configuration)
- [The health endpoint](#the-health-endpoint)
- [Adding a new endpoint](#adding-a-new-endpoint)

## What kind of API this is

[FORCA] Command Grid is **primarily a server-rendered Django application**, not a
REST/JSON API product. Almost every route in `config/urls.py` resolves to a Django
view (`apps/*/views.py`) that renders an HTML template (full page or an htmx partial),
not a machine-consumable JSON resource. Read this page as a map of *where the routes
live and how they're named*, not as a REST endpoint catalog — there is no separate
public API to version or document beyond what's described here.

## URL namespace map

`config/urls.py` is the root URLconf. Each entry mounts one app's `urls.py` under a
namespace (set via that app's `app_name = "..."`) at a fixed prefix:

| Namespace | Prefix | App |
|---|---|---|
| (none) | `/` | `config.views` (landing page) and `/healthz` |
| `sso` | `/auth/eve/` | `apps.sso` |
| `identity` | `/` | `apps.identity` |
| `killboard` | `/killboard/` | `apps.killboard` |
| `doctrines` | `/doctrines/` | `apps.doctrines` |
| `planetary` | `/industry/pi/` | `apps.planetary` |
| `industry` | `/industry/` | `apps.industry` |
| `skills` | `/skills/` | `apps.skills` |
| `stockpile` | `/stockpile/` | `apps.stockpile` |
| `market` | `/market/` | `apps.market` |
| `onboarding` | `/onboarding/` | `apps.onboarding` |
| `mentorship` | `/mentorship/` | `apps.mentorship` |
| `recommendations` | `/recommendations/` | `apps.recommendations` |
| `pingboard` | `/pingboard/` | `apps.pingboard` |
| `admin_audit` | `/ops/` | `apps.admin_audit` (the native admin console) |
| `pilots` | `/pilots/` | `apps.pilots` |
| `tasks` | `/tasks/` | `apps.tasks` |
| `srp` | `/srp/` | `apps.srp` |
| `raffle` | `/raffle/` | `apps.raffle` |
| `readiness` | `/readiness/` | `apps.readiness` |
| `operations` | `/operations/` | `apps.operations` |
| `mining` | `/mining/` | `apps.mining` |
| `erp` | `/erp/` | `apps.erp` |
| `kb` | `/kb/` | `apps.kb` |
| `recruitment` | `/recruitment/` | `apps.recruitment` |
| `corporation` | `/roster/` | `apps.corporation` |
| `logistics` | `/freight/` | `apps.logistics` |
| `buyback` | `/buyback/` | `apps.buyback` |
| `store` | `/store/` | `apps.store` |
| `navigation` | `/tools/` | `apps.navigation` |
| `command_intel` | `/command/` | `apps.command_intel` |
| `comms_access` | `/comms/` | `apps.comms_access` |
| (none, conditional) | `/admin/` | Stock Django admin, mounted only when `ENABLE_DJANGO_ADMIN` is true (off by default in production — see [security-guidelines.md](./security-guidelines.md)). |

Within an app, `urls.py` names each route (`name="..."`) and views are reached in
templates/redirects as `{namespace}:{name}`, e.g. `killboard:pilot`,
`store:system_search`, `readiness:dashboard`. See `core/features.py`'s
`_NAMESPACE_FEATURE` / `_VIEW_FEATURE` maps for which namespaces and individual views
are gated behind a leadership-configurable feature flag.

## HTML views (the primary surface)

Every namespace above is a collection of ordinary Django views returning
`render(request, template, context)`. Access control is applied per view with
`core.rbac` decorators/DRF permission classes (`role_required`, `perm_required`,
`IsMember`/`IsOfficer`/`IsDirector`/`IsAdmin`) or `core.features.feature_required`,
layered under the global `FeatureGateMiddleware` and `MembershipGateMiddleware`
described in [architecture.md](./architecture.md). Templates live under `templates/`
in a directory matching the namespace (e.g. `templates/killboard/`,
`templates/readiness/`).

## JSON and htmx partial endpoints

Two flavours of non-full-page response exist, both still ordinary Django views (not
DRF views):

- **htmx partials** — a view returns a template fragment intended to replace part of
  a page in place (an `hx-get`/`hx-post` target). By convention these templates carry
  a leading underscore, e.g. `templates/killboard/_feed.html`.
- **Plain JSON endpoints** — a small number of views return `JsonResponse` directly
  for autocomplete-style lookups consumed by client-side JS, for example
  `apps.store.views.hull_search` and `apps.store.views.system_search`
  (`store:hull_search`, `store:system_search`) and `apps.killboard.views.system_search`
  (`killboard:system_search`). These are plain Django views using
  `django.http.JsonResponse`, gated by the same role/feature checks as any other view
  (e.g. `hull_search` returns HTTP 403 as a JSON empty list, not an HTML error page, if
  the caller lacks access) — they are not part of the DRF/OpenAPI surface described
  below.

## DRF and OpenAPI schema configuration

`rest_framework` and `drf_spectacular` are installed and configured in
`config/settings/base.py`:

```python
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
}
SPECTACULAR_SETTINGS = {
    "TITLE": "[FORCA] Command Grid API",
    "DESCRIPTION": "Internal API for the FORCA Command Grid operations hub.",
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
}
```

This establishes the project-wide defaults any DRF view or viewset would inherit
(session-cookie authentication rather than a separate API token scheme,
`IsAuthenticated` by default, 50-row pages) and the schema generator
(`drf_spectacular.openapi.AutoSchema`) that would introspect them into an OpenAPI
document. As of this handbook, no app mounts a DRF `APIView`/`ViewSet` route, and no
URL serves the generated schema (`SERVE_INCLUDE_SCHEMA` is `False` and no
`SpectacularAPIView`/`SpectacularSwaggerView` is wired into `config/urls.py` or any
app's `urls.py`). The configuration is the established pattern for a DRF-backed
endpoint: add the view/serializer under the owning app, let
`drf_spectacular.openapi.AutoSchema` derive its schema automatically from the
serializer and view, and mount a schema/swagger URL when there is a concrete
consumer for it.

## The health endpoint

`GET /healthz` (`config/views.py::healthz`, no namespace) runs `SELECT 1` against the
database and returns `{"status": "ok", "database": true}` (HTTP 200) or
`{"status": "degraded", "database": false}` (HTTP 503). It never raises, and it is the
only endpoint exempted from the production HTTPS redirect
(`SECURE_REDIRECT_EXEMPT = [r"^healthz$"]`) so a local HTTP probe from a container
orchestrator still succeeds. See [architecture.md](./architecture.md#error-handling-and-logging).

## Adding a new endpoint

- **A new page or admin-console screen**: add a view + template in the owning app,
  wire it into that app's `urls.py`, and gate it with the appropriate `core.rbac`
  decorator and/or `core.features.feature_required`.
- **A new JSON/autocomplete endpoint**: follow the `store.hull_search` /
  `store.system_search` pattern — a plain view returning `JsonResponse`, with the same
  access checks any other view in that namespace uses. There is no need to introduce
  DRF for a small, view-specific JSON response.
- **A genuinely reusable, machine-consumed API resource**: use DRF (an `APIView` or
  `ViewSet` with a serializer) so it inherits the project defaults above and is picked
  up automatically by `drf_spectacular.openapi.AutoSchema`; then mount a schema URL if
  external consumers need the generated OpenAPI document.
