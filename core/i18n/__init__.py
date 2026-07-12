"""FORCA localisation (i18n) runtime.

Central home for the localisation machinery: locale resolution, the custom
profile-/impersonation-aware ``LocaleMiddleware``, the leader-configurable
``i18n.config`` accessor + kill switch, and language-scoped cache keys.

Design: docs/i18n/03-decisions.md (D3-D9, D17) and
docs/i18n/design/02-architecture.md. ``LocaleMiddleware`` is re-exported here so
the ``MIDDLEWARE`` dotted path is the tidy ``core.i18n.LocaleMiddleware``.
"""
from __future__ import annotations

from .cache import i18n_cache_key
from .config import (
    available_locales,
    broadcast_locale,
    default_locale,
    enabled_locales,
    get_i18n_config,
    is_i18n_enabled,
    set_i18n_config,
)
from .middleware import LocaleMiddleware
from .resolver import resolve_language

__all__ = [
    "LocaleMiddleware",
    "resolve_language",
    "i18n_cache_key",
    "get_i18n_config",
    "set_i18n_config",
    "enabled_locales",
    "available_locales",
    "default_locale",
    "broadcast_locale",
    "is_i18n_enabled",
]
