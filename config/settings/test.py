"""Test settings — fast, deterministic, runs against the Docker Postgres."""
from __future__ import annotations

import base64
import hashlib

from .base import *  # noqa: F401,F403

DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]

# Run Celery tasks inline during tests.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Fast password hashing for tests.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Local-memory cache so tests don't need Redis.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Deterministic, always-valid Fernet key for token-encryption tests.
TOKEN_ENCRYPTION_KEY = base64.urlsafe_b64encode(
    hashlib.sha256(b"forca-test-seed").digest()
).decode()

# Known test config for SSO/ESI.
EVE_SSO_CLIENT_ID = "test-client-id"
EVE_SSO_CLIENT_SECRET = "test-client-secret"
FORCA_HOME_CORP_ID = 98000001
