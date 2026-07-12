"""Deterministic active-locale resolution (docs/i18n/03-decisions.md D5-D7).

Precedence:

  * Authenticated: operator profile → language cookie → ``Accept-Language`` → default
  * Anonymous:                        language cookie → ``Accept-Language`` → default

The "operator" is ``request.real_user`` — the human at the browser — so a
director's "view-as" (impersonation) session renders in the *director's* language,
not the impersonated pilot's (D6). Every candidate is validated against the enabled
allow-list via ``get_supported_language_variant``; anything unknown, disabled, or
malformed (e.g. a path-traversal probe) is skipped — a locale value never reaches
the filesystem unvalidated.

Note (Django ≥ 4.0): session-based locale storage was removed from the framework,
so the persisted anonymous/explicit choice lives in the language **cookie**, not
the session (the design docs' "session" tier is realised as this cookie).
"""
from __future__ import annotations

from django.conf import settings
from django.utils.translation import get_supported_language_variant
from django.utils.translation.trans_real import parse_accept_lang_header

from .config import default_locale, enabled_locales, get_i18n_config, is_i18n_enabled


def _supported(code, allowed: set[str]) -> str | None:
    """Return the allow-listed variant of ``code`` (e.g. ``pt-BR`` → ``pt-br``) or None."""
    if not code or not isinstance(code, str):
        return None
    try:
        variant = get_supported_language_variant(code.replace("_", "-"))
    except (LookupError, TypeError, ValueError):
        return None
    return variant if variant in allowed else None


def _from_accept_language(request, allowed: set[str]) -> str | None:
    header = request.META.get("HTTP_ACCEPT_LANGUAGE", "")
    for code, _priority in parse_accept_lang_header(header):
        if code == "*":
            continue
        hit = _supported(code, allowed)
        if hit:
            return hit
    return None


def resolve_language(request) -> str:
    """Resolve the active UI language for ``request``."""
    fallback = settings.LANGUAGE_CODE
    if not is_i18n_enabled():
        return fallback

    allowed = set(enabled_locales())

    # 1. Authenticated operator's stored preference (impersonation-aware).
    operator = getattr(request, "real_user", None) or getattr(request, "user", None)
    if getattr(operator, "is_authenticated", False):
        hit = _supported(getattr(operator, "language", "") or "", allowed)
        if hit:
            return hit

    # 2. Explicit language cookie (set by the selector; also carries an anon choice).
    hit = _supported(request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME), allowed)
    if hit:
        return hit

    # 3. Browser Accept-Language, only if leadership left detection enabled.
    if get_i18n_config().get("browser_detection", True):
        hit = _from_accept_language(request, allowed)
        if hit:
            return hit

    # 4. The corp-configured default locale (English unless leadership changed it in
    #    the admin panel), validated against the enabled allow-list; a matching browser
    #    language above still wins. English is the ultimate fallback.
    return _supported(default_locale(), allowed) or _supported(fallback, allowed) or "en"
