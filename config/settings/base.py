"""Base settings shared by all environments.

Environment-specific overrides live in dev.py / prod.py / test.py.
Secrets and connection strings come from the environment (see deploy/.env.example).
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse as _urlparse

import environ
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
# Load a local .env if present (dev convenience; prod uses real env vars).
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    environ.Env.read_env(str(_env_file))

# --- Core ---------------------------------------------------------------
SECRET_KEY = env("DJANGO_SECRET_KEY", default="dev-insecure-key-do-not-use-in-prod")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
# Mount the stock Django admin? On in dev/test (handy for local inspection); prod.py
# turns it OFF by default since the native /ops/ console supersedes it. Env-overridable.
ENABLE_DJANGO_ADMIN = env.bool("DJANGO_ENABLE_ADMIN", default=True)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",  # intcomma for ISK display
    # third party
    "rest_framework",
    "drf_spectacular",
    # local — one app per bounded context
    "apps.sde",
    "apps.identity",
    "apps.sso",
    "apps.characters",
    "apps.corporation",
    "apps.killboard",
    "apps.doctrines",
    "apps.skills",
    "apps.industry",
    "apps.market",
    "apps.stockpile",
    "apps.onboarding",
    "apps.recommendations",
    "apps.admin_audit",
    "apps.pilots",
    "apps.tasks",
    "apps.srp",
    "apps.readiness",
    "apps.operations",
    "apps.erp",
    "apps.kb",
    "apps.recruitment",
    "apps.logistics",
    "apps.buyback",
    "apps.store",
    "apps.navigation",
    "apps.mining",
    "apps.command_intel",
    "apps.mentorship",
    "apps.planetary",
    "apps.pingboard",
    "apps.raffle",
    "apps.comms_access",
    "apps.impersonation",
    "apps.campaigns",
    "apps.capsuleer",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Immediately after AuthenticationMiddleware (needs request.user + session), and BEFORE
    # the membership/feature gates + the roles context processor: when a director has an
    # active "view-as" session, swap request.user for the impersonated pilot so every
    # downstream gate/view/nav transparently sees the pilot. Enforces view-only + live
    # re-validation; see apps.impersonation.middleware.
    "apps.impersonation.middleware.ImpersonationMiddleware",
    # After AuthenticationMiddleware (needs request.user + session): enforce an
    # absolute session lifetime cap over the sliding idle timeout.
    "core.middleware.AbsoluteSessionTimeoutMiddleware",
    # Must run after AuthenticationMiddleware (needs request.user): confines
    # logged-in non-corp pilots to the recruitment surface.
    "core.middleware.MembershipGateMiddleware",
    # 404s views whose feature leadership has turned off (default: all enabled).
    "core.features.FeatureGateMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.SecurityHeadersMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context.roles",
                "core.context.version",
                "core.context.csp_nonce",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --- Database -----------------------------------------------------------
DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default="postgres://forca:forca@postgres:5432/forca",
    ),
}

# --- Auth ---------------------------------------------------------------
AUTH_USER_MODEL = "identity.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LOGIN_URL = "/auth/eve/login/"
LOGIN_REDIRECT_URL = "/dashboard/"
LOGOUT_REDIRECT_URL = "/"

# Absolute session lifetime ceiling (seconds), enforced by
# core.middleware.AbsoluteSessionTimeoutMiddleware ON TOP of the sliding idle timeout
# (SESSION_COOKIE_AGE). Bounds the replay window of a stolen, actively-used cookie —
# which a sliding idle timeout alone never closes. Default 7 days; re-login is a single
# EVE-SSO click. Set to 0 to disable the absolute cap. Overridable via the env.
SESSION_ABSOLUTE_MAX_AGE = env.int("DJANGO_SESSION_ABSOLUTE_MAX_AGE", default=7 * 24 * 60 * 60)

# Director "view-as" (impersonation) auto-exit cap, in minutes. A forgotten view-as session
# auto-ends after this long (re-validated every request regardless). See apps.impersonation.
IMPERSONATION_MAX_MINUTES = env.int("IMPERSONATION_MAX_MINUTES", default=30)

# --- i18n / tz ----------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# --- Static / media -----------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # Non-manifest in dev/test; prod.py swaps in the compressed-manifest backend.
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Cache / Redis ------------------------------------------------------
REDIS_URL = env("REDIS_URL", default="redis://redis:6379/0")
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        # Bound every Redis operation. Without these, redis-py blocks forever on a
        # socket read, so a routine Redis stall (BGSAVE fork pause, swap pressure, a
        # network blip) would hang every gunicorn thread — the ~6 cache GETs the
        # `core.context.roles` processor issues on each authenticated render turn a
        # blip into a site-wide hang. With timeouts it fails fast per request instead.
        # These pass straight through to the redis-py connection pool.
        "OPTIONS": {
            "socket_connect_timeout": 2,
            "socket_timeout": 3,
            "retry_on_timeout": True,
            "health_check_interval": 30,
        },
    }
}

# --- Celery -------------------------------------------------------------
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default=REDIS_URL)
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default=REDIS_URL)
# No task result is ever consumed (no AsyncResult/.get()/.ready(), no chord/group/
# chain), so storing one `celery-task-meta-*` key per execution on the shared
# request-path Redis is pure write/memory churn. Ignore results globally; opt an
# individual task back in only if a caller ever reads its result.
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TIMEZONE = "UTC"

# --- DRF / OpenAPI ------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
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

# --- Token encryption (OAuth refresh tokens at rest) --------------------
# 32-byte url-safe base64 Fernet key. In prod this MUST be set in the env.
TOKEN_ENCRYPTION_KEY = env("TOKEN_ENCRYPTION_KEY", default="")

# --- EVE SSO / ESI ------------------------------------------------------
EVE_SSO_CLIENT_ID = env("EVE_SSO_CLIENT_ID", default="")
EVE_SSO_CLIENT_SECRET = env("EVE_SSO_CLIENT_SECRET", default="")
EVE_SSO_CALLBACK_URL = env(
    "EVE_SSO_CALLBACK_URL", default="http://localhost:8000/auth/eve/callback/"
)
EVE_SSO_BASE = "https://login.eveonline.com"
EVE_SSO_AUTHORIZE_URL = f"{EVE_SSO_BASE}/v2/oauth/authorize/"
EVE_SSO_TOKEN_URL = f"{EVE_SSO_BASE}/v2/oauth/token"
EVE_SSO_METADATA_URL = f"{EVE_SSO_BASE}/.well-known/oauth-authorization-server"
EVE_SSO_ISSUERS = ["login.eveonline.com", "https://login.eveonline.com"]

# --- Recruitment SSO (the SECOND EVE application) -----------------------
# A separate registered EVE app with a dedicated, NON-login callback, used only
# to read a consenting recruitment candidate's skills + corp roles ONCE to
# produce derived vetting evidence. Tokens are never stored. It shares CCP's
# authorize/token/JWKS endpoints (EVE_SSO_* above) but binds to its own client
# id/secret so candidate reads never touch the member-login application. Empty
# id/secret => RECRUITMENT_SSO_ENABLED is False and the live ESI-link flow stays
# disabled (recruitment remains public-evidence-only, a serviceable state).
RECRUITMENT_SSO_CLIENT_ID = env("RECRUITMENT_SSO_CLIENT_ID", default="")
RECRUITMENT_SSO_CLIENT_SECRET = env("RECRUITMENT_SSO_CLIENT_SECRET", default="")
RECRUITMENT_SSO_CALLBACK_URL = env(
    "RECRUITMENT_SSO_CALLBACK_URL",
    default="http://localhost:8000/recruitment/oauth/callback/",
)
RECRUITMENT_SSO_ENABLED = bool(RECRUITMENT_SSO_CLIENT_ID and RECRUITMENT_SSO_CLIENT_SECRET)
# The read-only scopes a candidate consents to (a subset of the app's enabled
# scopes — skills + the character's corp roles, plus publicData for the token).
RECRUITMENT_SSO_SCOPES = [
    "publicData",
    "esi-skills.read_skills.v1",
    "esi-characters.read_corporation_roles.v1",
]

# --- External comms access sync (apps.comms_access) ---------------------
# Drives Discord (then Slack/Mumble) roles/groups from corp-membership + RBAC.
# The whole subsystem ships INERT and is fully **console-configurable** — leadership arms it
# (enable + credentials + platform + mappings) at /ops/admin/comms-access/ with no .env
# access. The Discord bot token + OAuth client are stored ENCRYPTED on the
# comms_access.PlatformCredential row and take precedence; the env vars below remain an
# optional fallback for env-based deployments (a console value always wins).
#   COMMS_ACCESS_ENABLED — ops HARD kill switch, defaults ON so the Director console governs;
#     checked before any config read, so setting it False hard-disables with an attribute
#     lookup only (the login hot path pays nothing when hard-off).
COMMS_ACCESS_ENABLED = env.bool("COMMS_ACCESS_ENABLED", default=True)
DISCORD_BOT_TOKEN = env("DISCORD_BOT_TOKEN", default="")  # fallback; console credential wins
DISCORD_OAUTH_CLIENT_ID = env("DISCORD_OAUTH_CLIENT_ID", default="")
DISCORD_OAUTH_CLIENT_SECRET = env("DISCORD_OAUTH_CLIENT_SECRET", default="")
DISCORD_OAUTH_CALLBACK_URL = env(
    "DISCORD_OAUTH_CALLBACK_URL",
    default="http://localhost:8000/comms/discord/callback/",
)
# Env-only convenience flag; the console credential path enables linking independently
# (see apps.comms_access.oauth.enabled / apps.comms_access.credentials).
DISCORD_OAUTH_ENABLED = bool(DISCORD_OAUTH_CLIENT_ID and DISCORD_OAUTH_CLIENT_SECRET)

# --- Command Intelligence / LLM provider --------------------------------
# The strategic intelligence subsystem (apps.command_intel) calls an external LLM
# ONLY from Celery workers (never a web request). Provider = MiniMax, via its
# OpenAI-compatible /v1/chat/completions endpoint. The SECRET stays in env and is
# never committed or logged; every non-secret runtime knob (model name, budgets,
# thresholds) lives in the command_intel.provider AppSetting config and is tunable
# without a deploy. Empty key => COMMAND_INTEL_ENABLED is False and the subsystem
# ships inert (deterministic-only, zero external egress) — the RECRUITMENT_SSO_ENABLED
# idiom above.
LLM_PROVIDER = env("LLM_PROVIDER", default="minimax")
LLM_API_KEY = env("LLM_API_KEY", default="")
LLM_MODEL = env("LLM_MODEL", default="MiniMax-M2.7")
LLM_BASE_URL = env("LLM_BASE_URL", default="https://api.minimax.io/v1")
LLM_TIMEOUT = env.int("LLM_TIMEOUT", default=120)
COMMAND_INTEL_ENABLED = bool(LLM_API_KEY)
# Outbound host allowlist (SSRF guard; mirrors the ESI_BASE_URL startup check): a
# poisoned LLM_BASE_URL can never exfiltrate corp data / the key to an arbitrary host.
LLM_ALLOWED_HOSTS = env.list("LLM_ALLOWED_HOSTS", default=["api.minimax.io"])
if LLM_BASE_URL:
    from urllib.parse import urlparse as _ci_urlparse

    _ci_llm = _ci_urlparse(LLM_BASE_URL)
    _ci_local = {"localhost", "127.0.0.1"}
    if _ci_llm.scheme != "https" and _ci_llm.hostname not in _ci_local:
        from django.core.exceptions import ImproperlyConfigured

        raise ImproperlyConfigured("LLM_BASE_URL must use https")
    if _ci_llm.hostname not in set(LLM_ALLOWED_HOSTS) | _ci_local:
        from django.core.exceptions import ImproperlyConfigured

        raise ImproperlyConfigured(f"LLM_BASE_URL host is not allowlisted: {_ci_llm.hostname!r}")

# Optional SECOND LLM provider tried when the primary is unavailable, behind the same
# breaker (4.14). Enabled only when both a key and base URL are set. Its host must be
# allowlisted too (same SSRF guard as the primary).
LLM_FALLBACK_PROVIDER = env("LLM_FALLBACK_PROVIDER", default="minimax")
LLM_FALLBACK_API_KEY = env("LLM_FALLBACK_API_KEY", default="")
LLM_FALLBACK_MODEL = env("LLM_FALLBACK_MODEL", default="")
LLM_FALLBACK_BASE_URL = env("LLM_FALLBACK_BASE_URL", default="")
LLM_FALLBACK_ALLOWED_HOSTS = env.list("LLM_FALLBACK_ALLOWED_HOSTS", default=LLM_ALLOWED_HOSTS)
if LLM_FALLBACK_BASE_URL:
    from urllib.parse import urlparse as _ci_fb_urlparse

    _ci_fb = _ci_fb_urlparse(LLM_FALLBACK_BASE_URL)
    _ci_fb_local = {"localhost", "127.0.0.1"}
    if _ci_fb.scheme != "https" and _ci_fb.hostname not in _ci_fb_local:
        from django.core.exceptions import ImproperlyConfigured

        raise ImproperlyConfigured("LLM_FALLBACK_BASE_URL must use https")
    if _ci_fb.hostname not in set(LLM_FALLBACK_ALLOWED_HOSTS) | _ci_fb_local:
        from django.core.exceptions import ImproperlyConfigured

        raise ImproperlyConfigured(
            f"LLM_FALLBACK_BASE_URL host is not allowlisted: {_ci_fb.hostname!r}"
        )
# Fail closed on a half-configured fallback (one of the pair set) so it isn't silently
# disabled while an operator believes it is armed.
if bool(LLM_FALLBACK_API_KEY) != bool(LLM_FALLBACK_BASE_URL):
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(
        "LLM fallback is half-configured: set BOTH LLM_FALLBACK_API_KEY and "
        "LLM_FALLBACK_BASE_URL, or neither."
    )
LLM_FALLBACK_ENABLED = bool(LLM_FALLBACK_API_KEY and LLM_FALLBACK_BASE_URL)

# --- Pingboard providers (Slack / Telegram / WhatsApp) ------------------
# Global provider API tokens live in env (never DB); each provider ships inert until
# its secret is set (the RECRUITMENT_SSO_ENABLED / COMMAND_INTEL_ENABLED idiom).
# Per-destination secrets (Discord webhook URLs) stay Fernet-encrypted on
# ChannelProvider. Every provider's fixed API host is allowlisted so a bearer token
# can never be POSTed to an attacker host (SSRF guard, checked in the adapter).
PINGBOARD_SLACK_BOT_TOKEN = env("PINGBOARD_SLACK_BOT_TOKEN", default="")
PINGBOARD_SLACK_ENABLED = bool(PINGBOARD_SLACK_BOT_TOKEN)
PINGBOARD_SLACK_ALLOWED_HOSTS = env.list(
    "PINGBOARD_SLACK_ALLOWED_HOSTS", default=["slack.com", "hooks.slack.com"]
)

PINGBOARD_TELEGRAM_BOT_TOKEN = env("PINGBOARD_TELEGRAM_BOT_TOKEN", default="")
PINGBOARD_TELEGRAM_ENABLED = bool(PINGBOARD_TELEGRAM_BOT_TOKEN)
PINGBOARD_TELEGRAM_ALLOWED_HOSTS = env.list(
    "PINGBOARD_TELEGRAM_ALLOWED_HOSTS", default=["api.telegram.org"]
)
# A secret embedded in the inbound Telegram webhook path so only Telegram can call it.
PINGBOARD_TELEGRAM_WEBHOOK_SECRET = env("PINGBOARD_TELEGRAM_WEBHOOK_SECRET", default="")
# Bot username (no @) — builds the t.me deep link pilots use to verify their Telegram.
PINGBOARD_TELEGRAM_BOT_USERNAME = env("PINGBOARD_TELEGRAM_BOT_USERNAME", default="")

# WhatsApp is provider-neutral: choose a backend and supply its creds ("none" = off).
PINGBOARD_WHATSAPP_BACKEND = env("PINGBOARD_WHATSAPP_BACKEND", default="none")  # none|meta|twilio
PINGBOARD_WHATSAPP_META_TOKEN = env("PINGBOARD_WHATSAPP_META_TOKEN", default="")
PINGBOARD_WHATSAPP_META_PHONE_ID = env("PINGBOARD_WHATSAPP_META_PHONE_ID", default="")
PINGBOARD_WHATSAPP_META_API_VERSION = env("PINGBOARD_WHATSAPP_META_API_VERSION", default="v21.0")
PINGBOARD_WHATSAPP_TWILIO_SID = env("PINGBOARD_WHATSAPP_TWILIO_SID", default="")
PINGBOARD_WHATSAPP_TWILIO_TOKEN = env("PINGBOARD_WHATSAPP_TWILIO_TOKEN", default="")
PINGBOARD_WHATSAPP_TWILIO_FROM = env("PINGBOARD_WHATSAPP_TWILIO_FROM", default="")
PINGBOARD_WHATSAPP_ENABLED = (
    PINGBOARD_WHATSAPP_BACKEND == "meta"
    and bool(PINGBOARD_WHATSAPP_META_TOKEN and PINGBOARD_WHATSAPP_META_PHONE_ID)
) or (
    PINGBOARD_WHATSAPP_BACKEND == "twilio"
    and bool(
        PINGBOARD_WHATSAPP_TWILIO_SID
        and PINGBOARD_WHATSAPP_TWILIO_TOKEN
        and PINGBOARD_WHATSAPP_TWILIO_FROM
    )
)
PINGBOARD_WHATSAPP_ALLOWED_HOSTS = env.list(
    "PINGBOARD_WHATSAPP_ALLOWED_HOSTS", default=["graph.facebook.com", "api.twilio.com"]
)

# Minimal scopes at MVP; the scope-management UI requests more per feature.
# Core member scopes requested at login: skills/queue (readiness & plans),
# own killmails (personal losses), implants (training estimates), corp killmails
# + membership (corp data once a Director authorises), and corporation roles so a
# pilot who is an in-game Director is auto-granted the app's Director role at
# login (see apps.sso.services.sync_roles_for_user). These are the app's baseline
# value; a fresh deploy gets them without extra .env tuning. Overridable via the
# env var. Every scope must be enabled on the CCP application.
EVE_SSO_DEFAULT_SCOPES = env.list(
    "EVE_SSO_DEFAULT_SCOPES",
    default=[
        "publicData",
        "esi-skills.read_skills.v1",
        "esi-skills.read_skillqueue.v1",
        "esi-killmails.read_killmails.v1",
        "esi-clones.read_implants.v1",
        "esi-killmails.read_corporation_killmails.v1",
        "esi-corporations.read_corporation_membership.v1",
        "esi-characters.read_corporation_roles.v1",
    ],
)
# Opt-in feature scopes a member can additionally grant from the scopes page.
# Keyed by feature; only these allowlisted scopes may be requested (never raw
# user input). Corp-asset reading needs the in-game Director role too.
EVE_SSO_FEATURE_SCOPES = {
    # Director: read corp assets (+ name private structures where docking allows).
    "corp_assets": [
        "esi-assets.read_corporation_assets.v1",
        "esi-universe.read_structures.v1",
    ],
    # Any pilot: read their own assets.
    "personal_assets": [
        "esi-assets.read_assets.v1",
        "esi-universe.read_structures.v1",
    ],
    # Any pilot: read their own running industry jobs + owned blueprints, so the
    # Industry Center can track personal production and match it to plans.
    "my_industry": [
        "esi-industry.read_character_jobs.v1",
        "esi-characters.read_blueprints.v1",
    ],
    # Director: read corp contracts, to verify courier (freight) deliveries
    # actually completed in-game before crediting the hauler.
    "corp_contracts": [
        "esi-contracts.read_corporation_contracts.v1",
    ],
    # Any pilot: read their own contracts, so their hauls can be verified even
    # when no corp-contracts token has been granted.
    "my_contracts": [
        "esi-contracts.read_character_contracts.v1",
    ],
    # Director: read the corp roster + member tracking (location/ship/last login).
    "corp_roster": [
        "esi-corporations.read_corporation_membership.v1",
        "esi-corporations.track_members.v1",
    ],
    # Director / Station Manager: list the corp's structures, to auto-import the
    # Ansiblex jump-bridge + cyno-beacon network (read_structures resolves names).
    "jump_network": [
        "esi-corporations.read_structures.v1",
        "esi-universe.read_structures.v1",
    ],
    # Director / Station Manager: monitor every corp structure — fuel level, state
    # and reinforcement timers (read_structures resolves the dockable names).
    "corp_structures": [
        "esi-corporations.read_structures.v1",
        "esi-universe.read_structures.v1",
    ],
    # Any pilot: search player structures they can dock at, for the freight
    # location picker (name a pickup/drop-off precisely so the hauler knows the
    # exact dock and their docking rights).
    "freight_search": [
        "esi-search.search_structures.v1",
        "esi-universe.read_structures.v1",
    ],
    # Director / role-holder: relay in-game notifications (structure attacks, war
    # declarations, sovereignty, moon extractions) to the site and Discord.
    "notifications": [
        "esi-characters.read_notifications.v1",
    ],
    # The character subscribed to the corp mailing lists: relay new corp/alliance
    # mailing-list mail headers to Discord.
    "mail_relay": [
        "esi-mail.read_mail.v1",
    ],
    # The director chosen as the readiness mail sender: send readiness alert e-mails
    # in-game from this character (sending mail needs no in-game corp role).
    "readiness_mail": [
        "esi-mail.send_mail.v1",
    ],
    # The director chosen as the Pingboard mail sender: send Pingboard alerts in-game
    # from this character. Its own feature (separate from readiness_mail) so the two
    # senders can differ and be enabled/audited independently.
    "pingboard_mail": [
        "esi-mail.send_mail.v1",
    ],
    # The FC's own fleet: read the live fleet roster to auto-record attendance (PAP)
    # for an operation. Granted by whoever boss-fleets.
    "fleet_tracking": [
        "esi-fleets.read_fleet.v1",
    ],
    # Accountant / Director: corp wallet balances + journal for the finance page.
    "corp_finance": [
        "esi-wallet.read_corporation_wallets.v1",
    ],
    # Role-holder: corp contacts → the member-facing blue/red standings board.
    "corp_contacts": [
        "esi-corporations.read_contacts.v1",
    ],
    # Station Manager / Director: scheduled moon extractions for the extraction calendar.
    "moon_mining": [
        "esi-industry.read_corporation_mining.v1",
    ],
    # Director / Factory Manager: the corp's owned blueprints (BPO/BPC, ME/TE) and
    # running industry jobs — so blueprint coverage and build-or-buy reflect what
    # the corp actually owns and has in production, not just the SDE.
    "corp_industry": [
        "esi-corporations.read_blueprints.v1",
        "esi-industry.read_corporation_jobs.v1",
    ],
    # Any pilot (opt-in): confirm presence during a scheduled mentorship session.
    # location + online are real-time only (ESI keeps no history), so they are
    # polled solely inside a booked session window and never stored — the one way
    # to corroborate "mentor and mentee actually flew together".
    "mentorship_presence": [
        "esi-location.read_location.v1",
        "esi-location.read_online.v1",
    ],
    # Director: read the pilot's own saved ship fittings, to import them as corp
    # doctrines. (ESI exposes character fittings only — there is no corp-fittings
    # endpoint — so doctrine import is seeded from a director's personal fits.)
    "fittings": [
        "esi-fittings.read_fittings.v1",
    ],
    # Any pilot (opt-in): import your live Planetary Industry colonies into the PI
    # planner so it can show your real layouts, spot issues and estimate production.
    # ESI PI data only refreshes when you open the colony in the client, so imports
    # can be stale — the UI says so.
    "planetary_industry": [
        "esi-planets.manage_planets.v1",
    ],
}

ESI_BASE_URL = env("ESI_BASE_URL", default="https://esi.evetech.net")
# Every authenticated ESI call attaches a pilot's bearer token, so the base URL
# must never resolve to an arbitrary host (a misconfigured/poisoned env var would
# otherwise exfiltrate tokens). Pin it to https + an explicit host allowlist at
# startup; localhost is permitted so tests/dev can point at a local mock.
_esi_parsed = _urlparse(ESI_BASE_URL)
_ESI_ALLOWED_HOSTS = {"esi.evetech.net", "localhost", "127.0.0.1"}
if _esi_parsed.hostname not in _ESI_ALLOWED_HOSTS:
    raise ImproperlyConfigured(f"ESI_BASE_URL host is not allowlisted: {_esi_parsed.hostname!r}")
if _esi_parsed.scheme != "https" and _esi_parsed.hostname not in {"localhost", "127.0.0.1"}:
    raise ImproperlyConfigured("ESI_BASE_URL must use https")
ESI_USER_AGENT = env(
    "ESI_USER_AGENT", default="forca-command-grid/0.1 (contact-not-set@example.com)"
)
# Pin the compatibility date (omitting it selects ESI's OLDEST behaviour).
ESI_COMPATIBILITY_DATE = env("ESI_COMPATIBILITY_DATE", default="2026-06-21")

# Base URL for EVE imagery (ship renders, type icons, portraits, corp/alliance
# logos). Defaults to CCP's public image server so dev/test work with no extra
# infrastructure; production points this at the same-origin ``/eveimg`` nginx
# proxy-cache (see deploy/nginx) so images are served from our edge, survive CCP
# blips, and don't pull in a third-party origin. The CSP img-src is derived from
# this value (see core.middleware), so changing it keeps the policy consistent.
EVE_IMAGE_BASE_URL = env("EVE_IMAGE_BASE_URL", default="https://images.evetech.net")

# Where ``mirror_type_images`` pulls the static type icons/renders FROM — always
# CCP's real image server (never the local ``/eveimg`` proxy, which would loop).
EVE_IMAGE_SOURCE_URL = env("EVE_IMAGE_SOURCE_URL", default="https://images.evetech.net")
# Local directory the mirror writes type icons/renders into, served directly by
# nginx (``/eveimg/types/...`` → file, else proxy fallback). In prod this is a
# shared volume mounted into web/worker (rw) and nginx (ro); see docker-compose.
EVE_IMAGE_MIRROR_DIR = env("EVE_IMAGE_MIRROR_DIR", default=str(BASE_DIR / "eveimg"))

FORCA_HOME_CORP_ID = env.int("FORCA_HOME_CORP_ID", default=0)
# Canonical public base URL (e.g. https://grid.example.com), used to build absolute links
# in messages delivered off-site (Discord/EVE-mail/candidate e-mails). Prefer this
# over request.get_host() for outbound links so a spoofed Host header can never poison
# a broadcast link. Empty => callers fall back to a request-relative/absolute URL.
FORCA_SITE_URL = env("FORCA_SITE_URL", default="")
# Display name of the corporation that owns the app (customers make their in-game
# courier contracts to it). Resolved from EveCorporation.name when known; this is
# the branding fallback.
FORCA_CORP_NAME = env("FORCA_CORP_NAME", default="Forças Armadas")

# --- Email + scheduled briefings ----------------------------------------
# Off by default: with no SMTP host configured the backend is the no-op console
# backend, and with no recipients the leadership-briefing digest task only posts
# to Discord (itself a no-op until a webhook channel is added). Nothing is sent
# anywhere until an operator opts in via the env.
EMAIL_BACKEND = env(
    "EMAIL_BACKEND",
    default="django.core.mail.backends.console.EmailBackend",
)
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="forca@localhost")
# Who receives the scheduled leadership-briefing digest by email (comma-separated).
FORCA_BRIEFING_EMAILS = env.list("FORCA_BRIEFING_EMAILS", default=[])

# --- Logging ------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"verbose": {"format": "{levelname} {asctime} {name} {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "verbose"}},
    "root": {"handlers": ["console"], "level": env("DJANGO_LOG_LEVEL", default="INFO")},
}
