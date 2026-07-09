# ESI Integration

## Table of contents

- [Overview](#overview)
- [OAuth2 authorization-code flow with PKCE](#oauth2-authorization-code-flow-with-pkce)
- [JWT validation](#jwt-validation)
- [Token encryption at rest](#token-encryption-at-rest)
- [The disciplined ESI client](#the-disciplined-esi-client)
- [The SSRF host allowlist](#the-ssrf-host-allowlist)
- [Scope catalog](#scope-catalog)
- [Role sync at login](#role-sync-at-login)
- [How to add a new ESI integration safely](#how-to-add-a-new-esi-integration-safely)

## Overview

All CCP ESI/SSO integration code lives under `core/esi/`:

| Module | Responsibility |
|---|---|
| `core/esi/oauth.py` | OAuth2 authorization-code + PKCE flow, JWT validation, token refresh/revocation. |
| `core/esi/tokens.py` | Fernet encryption/decryption of refresh and access tokens at rest. |
| `core/esi/client.py` | The single HTTP chokepoint (`ESIClient`) for all authenticated/public ESI calls. |
| `core/esi/ratelimit.py` | Error-budget (420) and token-bucket (429) guard state, kept in the cache. |
| `core/esi/names.py` | Public `/universe/ids/` and `/universe/names/` id↔name resolution. |
| `core/esi/adapters/zkill.py` | The zKillboard ingestion adapter used alongside official ESI killmail sync. |

As established in [architecture.md](./architecture.md#the-golden-rule-esi-and-llm-calls-only-from-celery-workers),
**none of this is ever invoked from a web request** — only from Celery tasks
(`apps/*/tasks.py`).

## OAuth2 authorization-code flow with PKCE

`core/esi/oauth.py` implements the flow directly (rather than depending on a
third-party EVE SSO library), so the project controls JWT validation and token
handling precisely:

- **`generate_pkce()`** produces an S256 `(code_verifier, code_challenge)` pair;
  `generate_state()` produces a CSRF state token. Both are generated with
  `secrets`/`hashlib`, never a predictable source.
- **`build_authorize_url()`** builds the redirect to `EVE_SSO_AUTHORIZE_URL` carrying
  `code_challenge`/`code_challenge_method=S256`, `state`, and the requested scopes.
- **`exchange_code()`** exchanges the authorization code + verifier for tokens
  (`TokenResponse`: access token, refresh token, expiry, token type).
- **`refresh_access_token()`** exchanges a stored refresh token for a fresh access
  token, retaining the old refresh token if CCP doesn't rotate it in the response.
- **`revoke_token()`** best-effort revokes a refresh token at CCP on logout/unlink; it
  never raises — local erasure of the stored ciphertext is the actual guarantee, CCP
  revocation is a courtesy on top of it.

The project runs **two** separate registered EVE applications behind the
`SSOClient` dataclass: the default **member-login** client
(`EVE_SSO_CLIENT_ID`/`SECRET`/`EVE_SSO_CALLBACK_URL`), and a second, read-only
**recruitment** client (`RECRUITMENT_SSO_CLIENT_ID`/`SECRET`/`RECRUITMENT_SSO_CALLBACK_URL`)
used only to read a consenting candidate's skills and corp roles once, with tokens
never stored. Every function that touches a client-bound value accepts an optional
`client: SSOClient | None` and falls back to the login app when omitted, so recruitment
reads never share a token/audience with member login.

## JWT validation

`validate_access_token()` verifies an EVE SSO JWT against CCP's published JWKS
(`PyJWKClient`, cached process-wide after first use), pinning `algorithms=["RS256"]`
explicitly (no algorithm-confusion surface), and requiring `exp`/`iss`/`sub`/`aud` to
be present. It then checks, in order:

1. **Issuer** — must be one of `EVE_SSO_ISSUERS`.
2. **Audience** — must contain both the literal `"EVE Online"` and the resolved
   client's `client_id` (checked manually, not via PyJWT's `verify_aud`, so the
   member-login and recruitment clients can each validate against their own id).
3. **`azp`** (authorized party) — if present, must equal the resolved client's
   `client_id`, closing the residual ambiguity of a shared-audience token being
   replayed against the wrong client.

`character_id_from_claims()` parses the `sub` claim (`CHARACTER:EVE:<id>`) and
`owner_hash_from_claims()` extracts the SSO owner hash used to detect character
transfer (see [domain-model.md](./domain-model.md#eve-sso-integration)).

## Token encryption at rest

`core/esi/tokens.py` wraps `cryptography.fernet.Fernet`, keyed by
`TOKEN_ENCRYPTION_KEY`:

- `apps.sso.models.AuthToken` never stores or exposes plaintext: `refresh_token`/
  `access_token` are Python properties over the `_refresh_token`/`_access_token`
  database columns, encrypting on write and decrypting on read via
  `core.esi.tokens.encrypt`/`decrypt`.
- In production, `TOKEN_ENCRYPTION_KEY` is **required** — `config/settings/prod.py`
  raises `ImproperlyConfigured` at boot if it's unset.
- In development only, `ALLOW_DERIVED_TOKEN_KEY = True` (set explicitly in
  `config/settings/dev.py`, never implied by `DEBUG` alone) permits deriving a Fernet
  key from `SECRET_KEY` when no `TOKEN_ENCRYPTION_KEY` is configured, so a fresh dev
  checkout works without extra setup while production can never silently fall back to
  a weaker, guessable key.

## The disciplined ESI client

`core/esi/client.py`'s `ESIClient` is the single chokepoint for every ESI HTTP call
and enforces "good citizen" behaviour:

- **Pinned `X-Compatibility-Date`** (`ESI_COMPATIBILITY_DATE`) and a descriptive
  **`User-Agent`** (`ESI_USER_AGENT`) on every request — omitting the compatibility
  date would silently opt into ESI's oldest (and eventually removed) behaviour.
- **ETag caching**: successful `GET`s cache the response body keyed by ETag; a
  subsequent call sends `If-None-Match` and treats a `304` as a cache hit, avoiding
  redundant transfers and reducing the error-budget/rate-limit footprint.
- **Error-budget (420) and token-bucket (429) guards** (`core/esi/ratelimit.py`):
  before firing a non-essential call, `can_call()` checks whether ESI's remaining
  error budget (from `X-ESI-Error-Limit-Remain`/`-Reset` headers) is below
  `ERROR_BUDGET_FLOOR`, or whether a prior `429`'s `Retry-After` window is still
  active, and refuses the call (raising `ESIRateLimited`) rather than making it.
  `essential=True` calls bypass the budget floor (but never a hard 429 block).
- **Backoff with jitter** on `5xx` responses (`_sleep_backoff`, capped at 8 seconds),
  retried up to `max_retries` (default 3) before raising `ESIError`.
- **`X-Pages` pagination** (`get_paged()`) transparently walks every page of a
  paginated collection endpoint.
- A parallel `post()` method applies the same budget/bucket guard and backoff
  behaviour for ESI's body-based endpoints (e.g. `/route/`).

## The SSRF host allowlist

Every outbound integration's base URL is validated against an explicit host allowlist
**at settings load time**, not just at call time, so a misconfigured or poisoned
environment variable can never silently redirect authenticated calls (carrying a
pilot's bearer token, or an LLM API key) to an attacker-controlled host:

- **`ESI_BASE_URL`** must resolve to `esi.evetech.net` (or `localhost`/`127.0.0.1` for
  local testing) and use `https` (except for the local exceptions) —
  `config/settings/base.py` raises `ImproperlyConfigured` at import time otherwise.
- **`LLM_BASE_URL`** / **`LLM_FALLBACK_BASE_URL`** are checked the same way against
  `LLM_ALLOWED_HOSTS`/`LLM_FALLBACK_ALLOWED_HOSTS`.
- Messaging providers (Discord, Slack, Telegram, WhatsApp) follow the identical
  pattern with their own allowlists (`PINGBOARD_SLACK_ALLOWED_HOSTS`,
  `PINGBOARD_TELEGRAM_ALLOWED_HOSTS`, `PINGBOARD_WHATSAPP_ALLOWED_HOSTS`).

See [security-guidelines.md](./security-guidelines.md) for the general SSRF principle
that applies to any new outbound integration.

## Scope catalog

`EVE_SSO_DEFAULT_SCOPES` (`config/settings/base.py`) lists the scopes requested at
every member's login — the app's baseline value with no extra `.env` tuning needed on
a fresh deploy (skills, skill queue, personal killmails, implants, corp killmails,
corp membership, corporation roles).

`EVE_SSO_FEATURE_SCOPES` is a dict of **opt-in** scopes a member can additionally
grant from the in-app scopes page, keyed by feature (`corp_assets`, `personal_assets`,
`my_industry`, `corp_contracts`, `my_contracts`, `corp_roster`, `jump_network`,
`corp_structures`, `freight_search`, `notifications`, `mail_relay`, `readiness_mail`,
`pingboard_mail`, `fleet_tracking`, `corp_finance`, `corp_contacts`, `moon_mining`,
`corp_industry`, `mentorship_presence`, `fittings`, `planetary_industry`, and more —
see the dict in `config/settings/base.py` for the current, authoritative list and each
scope's inline rationale). Only these allowlisted scopes may ever be requested; a
scope is never built from raw user input. `apps/sso/scopes.py` is the corresponding
catalog consumed by the scopes page, and **must stay in sync** with
`EVE_SSO_FEATURE_SCOPES` — every feature scope listed in settings should appear on the
data-driven `/auth/eve/scopes/` page.

## Role sync at login

`apps.sso.services.sync_roles_for_user` runs after a successful login/token exchange
and reconciles the platform's RBAC roles against ESI-reported facts: a character in
the configured home corporation (`FORCA_HOME_CORP_ID`) is granted the `member` role,
and a character holding the in-game Director role is auto-granted the platform's
`director` role. The periodic `sso.reconcile_director_roles` and `sso.reconcile_scopes`
beat tasks (see [background-jobs.md](./background-jobs.md)) keep this reconciled over
time as in-game roles change, bounded and staleness-filtered so the ESI cost doesn't
scale with total alt count.

## How to add a new ESI integration safely

1. **Add the scope** to `EVE_SSO_FEATURE_SCOPES` in `config/settings/base.py` (with a
   short inline comment on who grants it and why) and to the catalog in
   `apps/sso/scopes.py` so it appears on the scopes page.
2. **Write a Celery task** in the owning app's `tasks.py` that calls
   `core.esi.client.get_client()` (or the appropriate helper in `core/esi/`), persists
   the result to a `ProvenanceMixin`-based model, and handles `ESIError`/
   `ESIRateLimited` by logging and returning rather than propagating into a crashed
   worker.
3. **Register a beat schedule entry** in `config/celery.py` at or above the data's ESI
   cache TTL, staggered from other jobs (see [background-jobs.md](./background-jobs.md)).
4. **Never call it from a view.** Views read what the task already persisted, and show
   an "as of" freshness label via `core/freshness.py` if the data can go stale.
5. If the integration introduces a new outbound host, add an explicit allowlist check
   at settings load time, following the `ESI_BASE_URL`/`LLM_BASE_URL` pattern above.

See [permissions-and-roles.md](../permissions-and-roles.md) for the full scope and
role reference, and [background-jobs.md](./background-jobs.md) for scheduling
conventions.
