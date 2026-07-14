# Security Guidelines

## Table of contents

- [Purpose](#purpose)
- [Encrypted tokens and credentials](#encrypted-tokens-and-credentials)
- [RBAC: least privilege and dual control](#rbac-least-privilege-and-dual-control)
- [Content-Security-Policy: nonce, no CDN scripts](#content-security-policy-nonce-no-cdn-scripts)
- [SSRF allowlists on every outbound integration](#ssrf-allowlists-on-every-outbound-integration)
- [Hardened XML parsing](#hardened-xml-parsing)
- [CSRF and secure cookies](#csrf-and-secure-cookies)
- [Never log secrets](#never-log-secrets)
- [No secrets in code or tests](#no-secrets-in-code-or-tests)
- [Input validation and IDOR-safe scoping](#input-validation-and-idor-safe-scoping)
- [Separation of duties in money/reward flows](#separation-of-duties-in-moneyreward-flows)
- [How to add a new admin setting safely](#how-to-add-a-new-admin-setting-safely)

## Purpose

This page states the security principles a contributor is expected to follow when
changing this codebase. For the deployment-facing security posture and how to report
a vulnerability, see [SECURITY.md](../../SECURITY.md). For the data model those
principles protect, see [../data-and-privacy.md](../data-and-privacy.md) and
[../permissions-and-roles.md](../permissions-and-roles.md).

## Encrypted tokens and credentials

OAuth refresh/access tokens (`apps.sso.models.AuthToken`) and third-party integration
credentials (e.g. `apps.comms_access`'s Discord bot token/OAuth client secret) are
stored encrypted at rest with Fernet, keyed by `TOKEN_ENCRYPTION_KEY`. Never add a new
model field that stores a secret in plaintext: follow the existing pattern of an
underscore-prefixed database column plus a property that encrypts on write and
decrypts on read (`core.esi.tokens.encrypt`/`decrypt`), as `AuthToken.refresh_token`/
`access_token` do. See [esi-integration.md](./esi-integration.md#token-encryption-at-rest).

## RBAC: least privilege and dual control

Every view that touches corp-private or leadership-only data must be gated by
`core.rbac`: `role_required`/`perm_required` decorators, the `IsMember`/`IsOfficer`/
`IsDirector`/`IsAdmin` DRF permission classes, or a `has_role`/`has_perm` check. Prefer
the narrowest applicable check:

- A capability specific to one workflow (recruiting, running fleet ops) should use a
  **lateral permission** (`PERM_RECRUITMENT_MANAGE`, `PERM_FLEET_MANAGE`) rather than
  requiring full officer rank, so a recruiter or FC gets exactly the access their role
  needs and nothing more.
- Granting the `director` role is **dual control**: `core.rbac.requires_dual_control`
  marks it as requiring a second director's approval
  (`apps.identity.models.RoleChangeRequest`), so a single compromised director account
  can never unilaterally mint another director. Don't bypass this by writing a direct
  `RoleAssignment.objects.create(role=director_role, ...)` in a new code path — go
  through the request/approval flow.

See [../permissions-and-roles.md](../permissions-and-roles.md) for the full role tier
and capability reference.

## Content-Security-Policy: nonce, no CDN scripts

`core.middleware.SecurityHeadersMiddleware` builds a per-request nonce-based CSP (see
[architecture.md](./architecture.md#front-end-architecture)). When adding front-end
code:

- **Never add a third-party script `<script src="https://...">` tag.** Every runtime
  library (Alpine, htmx, Chart.js, svg-pan-zoom) is vendored under `static/js/vendor/`
  and rebuilt via `frontend/` (see [local-development.md](./local-development.md)).
  Adding a CDN dependency reopens the exact residual risk this project deliberately
  closed.
- **Stamp the per-request nonce** (`{{ csp_nonce }}`, from the `core.context.csp_nonce`
  context processor) on any new inline `<script>` block that embeds server-rendered
  data, so it is authorised by `script-src 'nonce-<value>'` rather than requiring
  `'unsafe-inline'`.
- If a new integration needs a new outbound origin, derive the CSP source from
  settings the way `_image_csp_source()`/`_sso_csp_source()` in `core/middleware.py`
  do, rather than hardcoding a host in the policy string.

## SSRF allowlists on every outbound integration

Every outbound HTTP call this application makes — ESI, the LLM provider, Discord/
Slack/Telegram/WhatsApp — validates its target host against an explicit allowlist,
checked at settings load time wherever the base URL is fully known ahead of time (see
[esi-integration.md](./esi-integration.md#the-ssrf-host-allowlist)). When adding a new
outbound integration:

1. Define an explicit `<INTEGRATION>_ALLOWED_HOSTS` setting (or hardcode the one
   legitimate host if there is only ever one).
2. Validate the configured base URL's scheme (`https`, with a `localhost`/`127.0.0.1`
   exception for local development/testing only) and hostname against that allowlist,
   raising `ImproperlyConfigured` at import time on a mismatch — following the
   `ESI_BASE_URL`/`LLM_BASE_URL` checks in `config/settings/base.py` line for line.
3. Never build a request URL from unvalidated user input or a stored value that
   wasn't itself validated against the allowlist at write time.

## Hardened XML parsing

The EVE-client fitting XML importer (`apps.doctrines`) parses untrusted, user-supplied
XML. It uses `defusedxml` rather than the standard library's `xml.etree`/`lxml`
parsers, which are vulnerable to XXE, billion-laughs entity expansion, and external
entity/DTD resolution. Any new code path that parses XML (or any other data format
with a history of parser-level vulnerabilities) from an untrusted source must use the
hardened equivalent, not the naive standard-library parser.

## CSRF and secure cookies

Django's CSRF middleware is enabled project-wide; forms must include `{% csrf_token
%}`. In production (`config/settings/prod.py`), session and CSRF cookies are `Secure`,
`HttpOnly` (session) / readable-for-templates (CSRF, since `{% csrf_token %}` needs
it), and `SameSite=Lax`, with `SECURE_SSL_REDIRECT`, HSTS (with `includeSubDomains`
and `preload`), and `X-Frame-Options: DENY`. Don't weaken any of these in application
code (e.g. by adding `@csrf_exempt` to a state-changing view) without a specific,
reviewed reason.

The language selector adds a third cookie. It is named `forca_language` rather than
Django's stock `django_language`, lasts a year, and is `SameSite=Lax` and `HttpOnly`
(`config/settings/base.py`). `HttpOnly` is a deliberate deviation from Django's
default, which leaves the language cookie script-readable; nothing here reads it from
JavaScript — only the server-side resolver (`core/i18n/resolver.py`) does — so the XSS
read/write path that default leaves open is closed. In production
`LANGUAGE_COOKIE_SECURE` is read from `DJANGO_LANGUAGE_COOKIE_SECURE`
(`config/settings/prod.py`) and defaults to whatever `SESSION_COOKIE_SECURE` is. Don't
turn `HttpOnly` or `Secure` back off, and don't add client-side code that reads or
writes this cookie.

## Never log secrets

Never pass a token, refresh token, API key, webhook URL, or client secret to a
logging call, an exception message that might be logged, or `core.audit.audit_log`'s
`metadata`. `core.esi.oauth.revoke_token` and similar functions are written to log the
*outcome* ("revoke failed, continuing") without the token value itself — follow that
pattern for any new integration.

## No secrets in code or tests

- `.env` is git-ignored and must stay that way; `.env.example` (root) documents every
  variable with a placeholder value, never a real one.
- Test fixtures use obviously-fake values (`config/settings/test.py`'s deterministic
  Fernet key derived from a fixed test seed, `test-client-id`/`test-client-secret`).
  Follow that convention for any new test — never copy a real credential "just for
  testing," even temporarily.
- `ruff`'s `S` (bandit) rule set flags hardcoded-password-like patterns; the
  project-wide ignore only exempts `tests/*`/`**/tests/*`/`*/settings/*` for the
  specific `S105`/`S106` checks, not secrets in general.

## Input validation and IDOR-safe scoping

Every query that fetches a specific object by id from user input (a URL path
parameter, a POST field) must scope it to what the requesting user is actually allowed
to see — filter by the owning user, character, or corporation, not just the primary
key. This project's audit history includes fixes for exactly this class of bug (an
owner-hash/id comparison that didn't scope correctly); when writing a new detail view
or officer action, scope the queryset explicitly rather than trusting an id alone. The
same rule applies to a code that arrives in a POST and is then persisted rather than
used to look something up: `core/i18n/views.py`'s `set_language` re-derives the locale
from the enabled allow-list instead of echoing the posted string back, so the value
written to the language cookie and to `identity.User.language` is always one of the
codes from `settings.LANGUAGES`, never bytes off the wire. Follow that shape for any
new endpoint that stores a user-chosen code.

## Separation of duties in money/reward flows

SRP claims, buyback payouts, and store fulfilment all separate the person who
*requests/reports* an action from the person who *approves/pays* it — a claimant
cannot self-approve their own SRP claim, for example. When extending one of these
flows (or adding a new one that moves ISK or grants a reward), preserve this
separation rather than collapsing request and approval into the same role/user check.

## How to add a new admin setting safely

Leadership-tunable configuration (feature toggles, thresholds, audience settings)
follows one consistent pattern, exemplified by `core.features` and
`apps.admin_audit.models.AppSetting`:

1. **Store the value in `AppSetting`** (`key: str`, `value: JSONField`), never a new
   ad hoc model or a settings-module constant, unless the value is genuinely
   environment/deployment-level (see [configuration-reference.md](../configuration-reference.md)
   for environment variables vs. database-stored settings).
2. **Read through a small accessor function** (like `core.features.feature_enabled`/
   `feature_audience`) that caches the value per-process with a short TTL and busts
   the cache key on save, so the per-request read is a cache hit, not a query.
3. **Add a console page** under `/ops/` (`apps.admin_audit`) or the owning app's own
   settings page, gated to the appropriate role (`officer`/`director` at minimum for
   anything security-relevant), rather than exposing it through the disabled stock
   Django admin.
4. **Write an audit log entry** (`core.audit.audit_log`) on every change, recording
   the actor, the setting key, and the old/new value in `metadata` — every sensitive
   admin action should be reconstructable after the fact. See
   [../permissions-and-roles.md](../permissions-and-roles.md) and
   [../data-and-privacy.md](../data-and-privacy.md) for how audit data itself is
   retained and protected.

See [SECURITY.md](../../SECURITY.md) for the project's overall security posture
summary and how to report a vulnerability.
