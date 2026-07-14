# Environment Variables Reference

A flat, alphabetical index of every environment variable [FORCA] Command Grid reads. For
grouped explanations, defaults, and security notes in context, see the
[Configuration Reference](../configuration-reference.md). The canonical, fully commented
template is [`.env.example`](../../.env.example).

Legend: **Req** = required in production; **Sec** = sensitive (treat as a secret).

## Table of contents

- [Core and database](#core-and-database)
- [EVE SSO, ESI, and imagery](#eve-sso-esi-and-imagery)
- [Optional integrations](#optional-integrations)

## Core and database

| Variable | Req | Sec | Default | Summary |
|---|:--:|:--:|---|---|
| `CELERY_BROKER_URL` | | ✅ | `REDIS_URL` | Celery broker URL |
| `CELERY_RESULT_BACKEND` | | ✅ | `REDIS_URL` | Result backend (results ignored globally) |
| `CELERY_TASK_ALWAYS_EAGER` | | | `False` | Run tasks inline (tests) |
| `DATABASE_URL` | ✅ | ✅ | `postgres://forca:forca@postgres:5432/forca` | PostgreSQL connection URL |
| `DJANGO_ALLOWED_HOSTS` | ✅ | | `localhost,127.0.0.1` | Host header allowlist |
| `DJANGO_CONN_MAX_AGE` | | | `60` | Persistent DB connection lifetime (s) |
| `DJANGO_CSRF_COOKIE_SECURE` | | | `True` | Secure flag on the CSRF cookie |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | | | derived | CSRF trusted origins |
| `DJANGO_DEBUG` | | | `0` | Debug mode (keep `0` in prod) |
| `DJANGO_ENABLE_ADMIN` | | | `0` (prod) | Mount the stock Django `/admin/` |
| `DJANGO_HSTS_SECONDS` | | | `31536000` | HSTS max-age |
| `DJANGO_LANGUAGE_COOKIE_SECURE` | | | `DJANGO_SESSION_COOKIE_SECURE` | Secure flag on the language cookie (`forca_language`) |
| `DJANGO_LOG_LEVEL` | | | `INFO` | Root log level |
| `DJANGO_SECRET_KEY` | ✅ | ✅ | dev-only insecure default | Django cryptographic secret |
| `DJANGO_SECURE_SSL_REDIRECT` | | | `True` | Redirect HTTP → HTTPS |
| `DJANGO_SESSION_ABSOLUTE_MAX_AGE` | | | `604800` | Absolute session lifetime ceiling (s) |
| `DJANGO_SESSION_COOKIE_AGE` | | | `43200` | Sliding idle session timeout (s) |
| `DJANGO_SESSION_COOKIE_SECURE` | | | `True` | Secure flag on the session cookie |
| `DJANGO_SETTINGS_MODULE` | | | `config.settings.dev` | Settings module to load |
| `I18N_ENABLED` | | | `True` | Localisation kill switch; `0` short-circuits locale resolution |
| `POSTGRES_DB` | ✅ | | `forca` | Database name |
| `POSTGRES_PASSWORD` | ✅ | ✅ | — | Database password |
| `POSTGRES_USER` | ✅ | | `forca` | Database user |
| `REDIS_PASSWORD` | ✅ | ✅ | — | Redis container password |
| `REDIS_URL` | ✅ | ✅ | `redis://redis:6379/0` | Cache + default broker URL |
| `TOKEN_ENCRYPTION_KEY` | ✅ | ✅ | — | Fernet key encrypting stored tokens/credentials |

> `I18N_ENABLED` is only the outermost gate. Which languages the selector actually offers is
> leadership-managed in the console at `/ops/admin/i18n/`, not in the environment; see
> [Localisation](../configuration-reference.md#localisation) in the Configuration Reference.

## EVE SSO, ESI, and imagery

| Variable | Req | Sec | Default | Summary |
|---|:--:|:--:|---|---|
| `ESI_BASE_URL` | | | `https://esi.evetech.net` | ESI base (host-allowlisted at startup) |
| `ESI_COMPATIBILITY_DATE` | | | `2026-06-21` | Pinned ESI behaviour date |
| `ESI_USER_AGENT` | | | placeholder | Identifies the app to CCP (set a real contact) |
| `EVE_IMAGE_BASE_URL` | | | `/eveimg` (prod) | Base URL for EVE imagery; drives image CSP |
| `EVE_IMAGE_MIRROR_DIR` | | | `<repo>/eveimg` | Local image mirror directory |
| `EVE_IMAGE_SOURCE_URL` | | | `https://images.evetech.net` | Source the mirror pulls from |
| `EVE_SSO_CALLBACK_URL` | ✅ | | localhost default | OAuth redirect URI (must match the EVE app) |
| `EVE_SSO_CLIENT_ID` | ✅ | | — | EVE application client id (login app) |
| `EVE_SSO_CLIENT_SECRET` | ✅ | ✅ | — | EVE application client secret |
| `EVE_SSO_DEFAULT_SCOPES` | | | baseline set | Login scopes |
| `FORCA_CORP_NAME` | | | `Forças Armadas` | Corp display/branding name |
| `FORCA_HOME_CORP_ID` | ✅ | | `0` | Home corporation EVE id |
| `FORCA_SITE_URL` | | | empty | Canonical public base URL for off-site links |
| `RECRUITMENT_SSO_CALLBACK_URL` | | | localhost default | Second (recruitment) app callback |
| `RECRUITMENT_SSO_CLIENT_ID` | | | empty | Second (recruitment) app client id |
| `RECRUITMENT_SSO_CLIENT_SECRET` | | ✅ | empty | Second (recruitment) app secret |

## Optional integrations

Each stays inert until its secret is set. Hosts are allowlisted for SSRF protection.

| Variable | Sec | Default | Summary |
|---|:--:|---|---|
| `COMMS_ACCESS_ENABLED` | | `1` | Hard kill switch for the Discord role-sync subsystem |
| `DISCORD_BOT_TOKEN` | ✅ | empty | Discord bot (Manage Roles) — env fallback |
| `DISCORD_OAUTH_CLIENT_ID` | | empty | Discord OAuth app id |
| `DISCORD_OAUTH_CLIENT_SECRET` | ✅ | empty | Discord OAuth secret |
| `DISCORD_OAUTH_CALLBACK_URL` | | localhost default | Discord OAuth redirect |
| `LLM_API_KEY` | ✅ | empty | Enables Command Intelligence when set |
| `LLM_PROVIDER` | | `minimax` | LLM provider label |
| `LLM_MODEL` | | `MiniMax-M2.7` | LLM model name |
| `LLM_BASE_URL` | | `https://api.minimax.io/v1` | LLM endpoint (HTTPS + allowlisted) |
| `LLM_ALLOWED_HOSTS` | | `api.minimax.io` | LLM outbound host allowlist |
| `LLM_TIMEOUT` | | `120` | LLM request timeout (s) |
| `LLM_FALLBACK_API_KEY` | ✅ | empty | Optional secondary LLM key (set with base URL) |
| `LLM_FALLBACK_BASE_URL` | | empty | Optional secondary LLM endpoint |
| `LLM_FALLBACK_MODEL` | | empty | Optional secondary LLM model |
| `LLM_FALLBACK_ALLOWED_HOSTS` | | `LLM_ALLOWED_HOSTS` | Secondary LLM host allowlist |
| `PINGBOARD_SLACK_BOT_TOKEN` | ✅ | empty | Slack posts/DMs |
| `PINGBOARD_TELEGRAM_BOT_TOKEN` | ✅ | empty | Telegram bot |
| `PINGBOARD_TELEGRAM_WEBHOOK_SECRET` | ✅ | empty | Secret in the inbound Telegram webhook path |
| `PINGBOARD_TELEGRAM_BOT_USERNAME` | | empty | Telegram bot username (t.me deep link) |
| `PINGBOARD_WHATSAPP_BACKEND` | | `none` | `none` \| `meta` \| `twilio` |
| `PINGBOARD_WHATSAPP_META_TOKEN` | ✅ | empty | Meta WhatsApp Cloud API token |
| `PINGBOARD_WHATSAPP_META_PHONE_ID` | | empty | Meta WhatsApp phone id |
| `PINGBOARD_WHATSAPP_TWILIO_SID` | ✅ | empty | Twilio account SID |
| `PINGBOARD_WHATSAPP_TWILIO_TOKEN` | ✅ | empty | Twilio auth token |
| `PINGBOARD_WHATSAPP_TWILIO_FROM` | | empty | Twilio WhatsApp sender |
| `EMAIL_HOST` | | empty | SMTP host (empty ⇒ console backend) |
| `EMAIL_PORT` | | `587` | SMTP port |
| `EMAIL_HOST_USER` | | empty | SMTP user |
| `EMAIL_HOST_PASSWORD` | ✅ | empty | SMTP password |
| `EMAIL_USE_TLS` | | `True` | Use STARTTLS |
| `DEFAULT_FROM_EMAIL` | | `forca@localhost` | Default from address |
| `FORCA_BRIEFING_EMAILS` | | empty | Recipients of the scheduled leadership briefing |

> Some provider host-allowlist variables (`PINGBOARD_SLACK_ALLOWED_HOSTS`,
> `PINGBOARD_TELEGRAM_ALLOWED_HOSTS`, `PINGBOARD_WHATSAPP_ALLOWED_HOSTS`,
> `PINGBOARD_WHATSAPP_META_API_VERSION`) have working defaults and rarely need changing;
> see [`config/settings/base.py`](../../config/settings/base.py).
