"""Production settings — security-hardened."""
from __future__ import annotations

import os

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401,F403
from .base import DATABASES, env

DEBUG = False

# Report EVERY missing required variable at once. Reading them one at a time (as the
# assignments below do) surfaces only the first, so an operator fixes one, reboots,
# and discovers the next — once per restart.
_REQUIRED = {
    "DJANGO_SECRET_KEY": "Django cryptographic secret. Generate: openssl rand -base64 50",
    "TOKEN_ENCRYPTION_KEY": "Fernet key for OAuth refresh tokens at rest. "
                            "Generate: openssl rand -base64 32 | tr '+/' '-_'",
    "DJANGO_ALLOWED_HOSTS": "Comma-separated Host header allowlist, e.g. grid.example.com",
    "DATABASE_URL": "postgres://user:password@postgres:5432/forca",
}
_missing = [f"  {name} — {why}" for name, why in _REQUIRED.items() if not os.environ.get(name)]
if _missing:
    raise ImproperlyConfigured(
        "Missing required environment variable(s) for config.settings.prod:\n"
        + "\n".join(_missing)
        + "\n\nSee .env.example. The deploy script generates these automatically."
    )

# The native /ops/ console (OFFICER/Director-gated) replaces the stock Django admin in
# production, so leave /admin/ unmounted by default — smaller attack surface, no
# guessable admin login. Set DJANGO_ENABLE_ADMIN=1 to re-enable a break-glass admin.
ENABLE_DJANGO_ADMIN = env.bool("DJANGO_ENABLE_ADMIN", default=False)


# --- Database connections ----------------------------------------------
# Persist a connection across requests instead of opening/closing one per
# request (Django's default CONN_MAX_AGE=0). CONN_HEALTH_CHECKS pings a reused
# connection before use so a server-side drop never surfaces as a 500. Each
# gunicorn thread / celery process keeps at most one connection, so total usage
# stays well under Postgres max_connections. Tunable via env.
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DJANGO_CONN_MAX_AGE", default=60)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True

# Serve EVE imagery from our own edge by default: the nginx /eveimg proxy-cache
# (see deploy/nginx/forca.prod.conf) fronts CCP's image server, so pages stay
# same-origin and survive upstream blips. Overridable via the env var.
EVE_IMAGE_BASE_URL = env("EVE_IMAGE_BASE_URL", default="/eveimg")

# Required in production — fail loudly if missing.
SECRET_KEY = env("DJANGO_SECRET_KEY")
TOKEN_ENCRYPTION_KEY = env("TOKEN_ENCRYPTION_KEY")
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")

# CSRF trusted origins: explicit env value wins; otherwise derive https://<host>
# for each real (non-loopback) allowed host so same-origin HTTPS POSTs are
# trusted out of the box rather than silently relying on Referer checks.
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[]) or [
    f"https://{host}"
    for host in ALLOWED_HOSTS
    if host not in ("127.0.0.1", "localhost") and not host.startswith("*")
]

# Refuse to boot production with the insecure development defaults.
if SECRET_KEY == "dev-insecure-key-do-not-use-in-prod":
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set to a real secret in production.")
if not TOKEN_ENCRYPTION_KEY:
    raise ImproperlyConfigured("TOKEN_ENCRYPTION_KEY must be set in production.")

# CCP's ESI policy requires every client to identify itself with a contactable
# address. Shipping a placeholder gets a self-hoster's IP rate-limited or blocked, and
# the failure looks like "ESI is down" rather than "your User-Agent is wrong". Cover
# both the settings default and the one the deploy script writes when --contact-email
# was omitted.
_UA_PLACEHOLDERS = ("contact-not-set@example.com", "set-a-contact-email", "you@example.com")
if any(p in ESI_USER_AGENT for p in _UA_PLACEHOLDERS):  # noqa: F405 - from .base import *
    raise ImproperlyConfigured(
        f"ESI_USER_AGENT still contains a placeholder contact address ({ESI_USER_AGENT!r}). "  # noqa: F405
        "Set a real one, e.g. ESI_USER_AGENT='forca-command-grid/1.0 (ops@yourcorp.com)'. "
        "CCP requires a contactable address and may block clients without one."
    )

# HTTPS / transport security. Secure by default; the cookie/redirect/HSTS
# flags are env-overridable so a behind-TLS deployment can be tuned (e.g. an
# internal HTTP test box can disable them) without weakening the defaults.
SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=True)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# The container healthcheck hits /healthz over plain HTTP on localhost (no proxy
# header), so exempt it from the HTTPS redirect — otherwise it is 301'd to an
# https URL gunicorn doesn't speak and the check fails.
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]
SECURE_HSTS_SECONDS = env.int("DJANGO_HSTS_SECONDS", default=31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

# Cookies
SESSION_COOKIE_SECURE = env.bool("DJANGO_SESSION_COOKIE_SECURE", default=True)
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = env.bool("DJANGO_CSRF_COOKIE_SECURE", default=True)
CSRF_COOKIE_HTTPONLY = False  # template forms need to read it via {% csrf_token %}
CSRF_COOKIE_SAMESITE = "Lax"
# The language cookie holds a locale code, not a credential, but there is no reason
# to ship it over plaintext when nothing else here is. Default to the session
# cookie's posture so an HTTP-only test box that already relaxed that one keeps
# working (a Secure cookie over HTTP is silently dropped, losing the selection).
LANGUAGE_COOKIE_SECURE = env.bool("DJANGO_LANGUAGE_COOKIE_SECURE", default=SESSION_COOKIE_SECURE)

# Session lifetime: cap the replay window of a stolen session cookie. Django's
# default is a 2-week absolute age with no idle timeout; for an ops hub holding
# corp-asset and Director data we use a shorter age with sliding idle expiry so
# an unused session ages out.
SESSION_COOKIE_AGE = env.int("DJANGO_SESSION_COOKIE_AGE", default=12 * 60 * 60)  # 12h
SESSION_SAVE_EVERY_REQUEST = True  # refresh the cookie on activity (idle timeout)

# Clickjacking
X_FRAME_OPTIONS = "DENY"

# Hashed, compressed static files in production.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
