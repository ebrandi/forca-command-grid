# Configuration Reference

This page documents every configuration input [FORCA] Command Grid reads: environment
variables, the settings modules that consume them, and the leadership-tunable settings
stored in the database. It is written for operators and contributors.

> **Safety:** All example values below are dummy placeholders. Never commit a real
> `.env`. Secrets are marked **[sensitive]**; treat them like passwords.

## Table of contents

- [How configuration is loaded](#how-configuration-is-loaded)
- [Settings modules](#settings-modules)
- [Environment variables](#environment-variables)
  - [Django core](#django-core)
  - [Database](#database)
  - [Redis and Celery](#redis-and-celery)
  - [Token encryption](#token-encryption)
  - [EVE SSO and ESI](#eve-sso-and-esi)
  - [Recruitment SSO](#recruitment-sso)
  - [Discord role sync](#discord-role-sync)
  - [Command Intelligence / LLM](#command-intelligence--llm)
  - [Pingboard alert channels](#pingboard-alert-channels)
  - [Email and briefings](#email-and-briefings)
- [Database-stored settings (leadership-tunable)](#database-stored-settings-leadership-tunable)
- [Feature flags and audiences](#feature-flags-and-audiences)

## How configuration is loaded

Configuration comes from environment variables, parsed by `django-environ` in
[`config/settings/base.py`](../config/settings/base.py). In development a `.env` file at
the repository root is loaded automatically; in production the same variables are supplied
by the environment (the deploy script writes them to `/opt/forca/app/.env`, mode `600`).

The canonical, fully commented template is [`.env.example`](../.env.example). Copy it to
`.env` and fill it in, or let the deploy script generate strong random secrets for you.

## Settings modules

| Module | Used for | Notes |
|---|---|---|
| `config.settings.base` | Shared defaults for all environments | Reads every environment variable. |
| `config.settings.dev` | Local development | `DEBUG` on, stock Django admin mounted. |
| `config.settings.prod` | Production | `DEBUG` off, HTTPS/HSTS/secure cookies, admin disabled by default, fails to boot without required secrets. |
| `config.settings.test` | Automated tests | Used by `pytest`. |

Select the module with `DJANGO_SETTINGS_MODULE` (default in production:
`config.settings.prod`).

## Environment variables

Legend: **[REQUIRED]** = production boot fails if unset; **[sensitive]** = secret.

### Django core

| Variable | Req. | Default | Purpose | Security |
|---|---|---|---|---|
| `DJANGO_SETTINGS_MODULE` | Recommended | `config.settings.dev` (compose sets prod) | Which settings module to load. | — |
| `DJANGO_SECRET_KEY` | **[REQUIRED]** | dev-only insecure default | Django cryptographic secret; prod refuses the insecure default. | **[sensitive]** |
| `DJANGO_DEBUG` | No | `0` | Debug mode. Must stay `0` in production. | Leaks stack traces if on. |
| `DJANGO_ALLOWED_HOSTS` | **[REQUIRED]** | `localhost,127.0.0.1` | Comma-separated Host header allowlist. | Prevents Host spoofing. |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | No | derived `https://<host>` | CSRF trusted origins (scheme+host). | — |
| `DJANGO_ENABLE_ADMIN` | No | `0` in prod / `1` in dev | Mount the stock Django `/admin/`. | Off by default reduces attack surface. |
| `DJANGO_SESSION_COOKIE_AGE` | No | `43200` (12h) | Sliding idle session timeout (seconds). | — |
| `DJANGO_SESSION_ABSOLUTE_MAX_AGE` | No | `604800` (7d) | Absolute session lifetime ceiling (seconds); `0` disables. | Bounds stolen-cookie replay. |
| `DJANGO_LOG_LEVEL` | No | `INFO` | Root log level. | — |
| `DJANGO_CONN_MAX_AGE` | No | `60` | Persistent DB connection lifetime (seconds). | — |
| `DJANGO_SECURE_SSL_REDIRECT` | No | `True` | Redirect HTTP→HTTPS. | Disable only on internal HTTP test boxes. |
| `DJANGO_HSTS_SECONDS` | No | `31536000` | HSTS max-age. | — |
| `DJANGO_SESSION_COOKIE_SECURE` / `DJANGO_CSRF_COOKIE_SECURE` | No | `True` | Secure cookie flags. | Keep on behind TLS. |

### Database

| Variable | Req. | Default | Purpose | Security |
|---|---|---|---|---|
| `DATABASE_URL` | **[REQUIRED]** (prod) | `postgres://forca:forca@postgres:5432/forca` | Full PostgreSQL connection URL. | **[sensitive]** (contains password) |
| `POSTGRES_DB` | Yes | `forca` | Database name (seeds the postgres container). | — |
| `POSTGRES_USER` | Yes | `forca` | Database user. | — |
| `POSTGRES_PASSWORD` | Yes | — | Database password; must match `DATABASE_URL`. | **[sensitive]** |

### Redis and Celery

| Variable | Req. | Default | Purpose | Security |
|---|---|---|---|---|
| `REDIS_PASSWORD` | Yes (prod) | — | Password for the Redis container. | **[sensitive]** |
| `REDIS_URL` | Yes | `redis://redis:6379/0` | Cache + default broker/result backend URL. | **[sensitive]** |
| `CELERY_BROKER_URL` | No | `REDIS_URL` | Celery broker (commonly Redis DB 1). | **[sensitive]** |
| `CELERY_RESULT_BACKEND` | No | `REDIS_URL` | Result backend (results are ignored globally). | **[sensitive]** |
| `CELERY_TASK_ALWAYS_EAGER` | No | `False` | Run tasks inline (tests only). | — |

### Token encryption

| Variable | Req. | Default | Purpose | Security |
|---|---|---|---|---|
| `TOKEN_ENCRYPTION_KEY` | **[REQUIRED]** | empty | Fernet key (url-safe base64, 32 bytes) encrypting stored OAuth refresh tokens and integration credentials. Losing it means members must re-authorise. | **[sensitive]** — back it up securely. |

### EVE SSO and ESI

| Variable | Req. | Default | Purpose | Security |
|---|---|---|---|---|
| `EVE_SSO_CLIENT_ID` | Yes (for login) | empty | EVE application client id (login app). | — |
| `EVE_SSO_CLIENT_SECRET` | Yes (for login) | empty | EVE application client secret. | **[sensitive]** |
| `EVE_SSO_CALLBACK_URL` | Yes | `http://localhost:8000/auth/eve/callback/` | Must equal the app's registered redirect URI. | — |
| `EVE_SSO_DEFAULT_SCOPES` | No | see [permissions-and-roles.md](./permissions-and-roles.md) | Baseline login scopes. | Every scope must be enabled on the CCP app. |
| `ESI_USER_AGENT` | Recommended | placeholder | Identifies the app to CCP (name/version + real contact). | Use a real contact email. |
| `ESI_COMPATIBILITY_DATE` | No | `2026-06-21` | Pins ESI behaviour. | Bump deliberately after testing. |
| `ESI_BASE_URL` | No | `https://esi.evetech.net` | ESI base; validated against an allowlist at startup. | SSRF guard — non-allowlisted host fails boot. |
| `EVE_IMAGE_BASE_URL` | No | `/eveimg` (prod) | Base URL for EVE imagery; drives the image CSP source. | — |
| `EVE_IMAGE_SOURCE_URL` | No | `https://images.evetech.net` | Source the image mirror pulls from. | — |
| `EVE_IMAGE_MIRROR_DIR` | No | `<repo>/eveimg` | Local dir the mirror writes to. | — |
| `FORCA_HOME_CORP_ID` | Yes | `0` | Home corporation EVE id (numeric). | — |
| `FORCA_SITE_URL` | Recommended | empty | Canonical public base URL for absolute links in off-site messages. | Prevents Host-header link poisoning. |
| `FORCA_CORP_NAME` | No | `Forças Armadas` | Corp display/branding name. | — |

### Recruitment SSO

A **second, optional** EVE application used only for read-only candidate vetting. Leave
blank to keep recruitment public-evidence-only.

| Variable | Req. | Default | Purpose | Security |
|---|---|---|---|---|
| `RECRUITMENT_SSO_CLIENT_ID` | No | empty | Second EVE app client id. | — |
| `RECRUITMENT_SSO_CLIENT_SECRET` | No | empty | Second EVE app secret. | **[sensitive]** |
| `RECRUITMENT_SSO_CALLBACK_URL` | No | `http://localhost:8000/recruitment/oauth/callback/` | Dedicated non-login callback. | Candidate tokens are never stored. |

### Discord role sync

The `comms_access` subsystem is configured primarily in the Admin Console
(`/ops/admin/comms-access/`); credentials there are stored **encrypted** and take
precedence. These environment variables are an optional fallback.

| Variable | Req. | Default | Purpose | Security |
|---|---|---|---|---|
| `COMMS_ACCESS_ENABLED` | No | `1` (on) | Hard kill switch; `0` fully disables the subsystem. | — |
| `DISCORD_BOT_TOKEN` | No | empty | Bot with Manage Roles on your guild (fallback). | **[sensitive]** |
| `DISCORD_OAUTH_CLIENT_ID` | No | empty | Discord OAuth app id (account linking). | — |
| `DISCORD_OAUTH_CLIENT_SECRET` | No | empty | Discord OAuth secret. | **[sensitive]** |
| `DISCORD_OAUTH_CALLBACK_URL` | No | `http://localhost:8000/comms/discord/callback/` | Discord OAuth redirect. | — |

### Command Intelligence / LLM

Optional strategic-AI features. An empty `LLM_API_KEY` disables the subsystem cleanly;
the LLM is only ever called from Celery workers.

| Variable | Req. | Default | Purpose | Security |
|---|---|---|---|---|
| `LLM_API_KEY` | No | empty | Enables Command Intelligence when set. | **[sensitive]** |
| `LLM_PROVIDER` | No | `minimax` | Provider label. | — |
| `LLM_MODEL` | No | `MiniMax-M2.7` | Model name. | — |
| `LLM_BASE_URL` | No | `https://api.minimax.io/v1` | Provider endpoint; must be HTTPS and allowlisted. | SSRF guard at startup. |
| `LLM_ALLOWED_HOSTS` | No | `api.minimax.io` | Outbound host allowlist. | — |
| `LLM_TIMEOUT` | No | `120` | Request timeout (seconds). | — |
| `LLM_FALLBACK_*` | No | empty | Optional secondary provider; set **both** key and base URL or neither. | Half-configured fallback fails boot. |

### Pingboard alert channels

Optional. Telegram and WhatsApp are fully console-configurable (encrypted per-channel);
these variables are a fallback. Each provider stays inert until its secret is set. Every
provider's API host is allowlisted (SSRF guard).

| Variable | Req. | Default | Purpose | Security |
|---|---|---|---|---|
| `PINGBOARD_SLACK_BOT_TOKEN` | No | empty | Slack posts/DMs. | **[sensitive]** |
| `PINGBOARD_TELEGRAM_BOT_TOKEN` | No | empty | Telegram bot. | **[sensitive]** |
| `PINGBOARD_TELEGRAM_WEBHOOK_SECRET` | No | empty | Secret embedded in the inbound webhook path. | **[sensitive]** |
| `PINGBOARD_TELEGRAM_BOT_USERNAME` | No | empty | Builds the t.me self-link deep link. | — |
| `PINGBOARD_WHATSAPP_BACKEND` | No | `none` | `none` \| `meta` \| `twilio`. | — |
| `PINGBOARD_WHATSAPP_META_TOKEN` / `PINGBOARD_WHATSAPP_META_PHONE_ID` | No | empty | Meta WhatsApp Cloud API. | **[sensitive]** (token) |
| `PINGBOARD_WHATSAPP_TWILIO_SID` / `_TOKEN` / `_FROM` | No | empty | Twilio WhatsApp. | **[sensitive]** |

### Email and briefings

Optional. With no `EMAIL_HOST`, Django uses the console backend (emails are logged, not
sent).

| Variable | Req. | Default | Purpose | Security |
|---|---|---|---|---|
| `EMAIL_HOST` | No | empty | SMTP host; empty ⇒ console backend. | — |
| `EMAIL_PORT` | No | `587` | SMTP port. | — |
| `EMAIL_HOST_USER` | No | empty | SMTP user. | — |
| `EMAIL_HOST_PASSWORD` | No | empty | SMTP password. | **[sensitive]** |
| `EMAIL_USE_TLS` | No | `True` | STARTTLS. | — |
| `DEFAULT_FROM_EMAIL` | No | `forca@localhost` | From address. | — |
| `FORCA_BRIEFING_EMAILS` | No | empty | Comma-separated recipients of the scheduled leadership briefing. | — |

## Database-stored settings (leadership-tunable)

Beyond environment variables, most day-to-day behaviour is configured **without a
redeploy** through the role-gated Admin Console at `/ops/`. These values are stored in the
database (many under an `AppSetting` key/value store) and edited by officers and
directors. Examples include:

- Feature enablement and per-service audiences (see below).
- Killboard combat-rank ladders, rewards, and kill-feed thresholds.
- SRP program payout modes and valuation.
- Readiness dimensions, weights, and alert rules.
- Pingboard channels, providers, and automation rules.
- Command Intelligence model/budget/threshold knobs.
- Data-retention windows and member-leave policy.
- Notification event routing and classification.

The full set of console sections is enumerated in the
[administrator handbook](./administrator-handbook/README.md).

## Feature flags and audiences

Member-facing features are **enabled by default**; leadership can turn any off, or set a
**4-state audience** (`disabled` / `corp` / `alliance` / `public`) for audience-controlled
features (doctrines, navigation, raffles, and the member services). This is managed at
**Admin Console → Services & features** (`/ops/admin/features/`). The mechanics — the
`FeatureGateMiddleware`, the feature catalogue, and audience resolution — are described in
[permissions-and-roles.md](./permissions-and-roles.md).
