"""Development settings."""
from __future__ import annotations

from .base import *  # noqa: F401,F403
from .base import env

DEBUG = True
# Concrete host list (not "*") even in dev, so a dev box on a routable network
# still rejects Host-header poisoning / DNS-rebind. Covers localhost and the
# docker-compose service name.
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]", "web"]
INTERNAL_IPS = ["127.0.0.1"]

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# In dev, TOKEN_ENCRYPTION_KEY may be left unset: core.esi.tokens then derives a
# valid Fernet key from SECRET_KEY. This is explicitly opted into here (never via
# DEBUG alone) so it can't accidentally weaken token encryption elsewhere. Prod
# requires a real key and never enables this fallback.
TOKEN_ENCRYPTION_KEY = env("TOKEN_ENCRYPTION_KEY", default="")
ALLOW_DERIVED_TOKEN_KEY = True
