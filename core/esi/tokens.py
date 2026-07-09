"""Encryption for OAuth refresh tokens at rest (Fernet)."""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def _derive_dev_key() -> bytes:
    """Deterministic Fernet key derived from SECRET_KEY (DEBUG/dev only)."""
    return base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())


def _derived_fallback_allowed() -> bool:
    """The SECRET_KEY-derived dev key is a last resort for local development only.

    It is gated on an explicit opt-in flag (default off) rather than ``DEBUG`` so
    that an accidental ``DEBUG=True`` can never silently downgrade at-rest token
    encryption to a key derivable from SECRET_KEY.
    """
    return bool(getattr(settings, "ALLOW_DERIVED_TOKEN_KEY", False))


def get_fernet() -> Fernet:
    key = settings.TOKEN_ENCRYPTION_KEY
    if key:
        try:
            return Fernet(key.encode() if isinstance(key, str) else key)
        except Exception as exc:  # noqa: BLE001
            if _derived_fallback_allowed():
                return Fernet(_derive_dev_key())
            raise ImproperlyConfigured("TOKEN_ENCRYPTION_KEY is not a valid Fernet key") from exc
    if _derived_fallback_allowed():
        return Fernet(_derive_dev_key())
    raise ImproperlyConfigured("TOKEN_ENCRYPTION_KEY must be set")


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    return get_fernet().decrypt(ciphertext.encode()).decode()
