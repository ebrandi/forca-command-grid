"""Template context for the language selector (native labels, no flags).

``django.template.context_processors.i18n`` already exposes ``LANGUAGE_CODE`` and
``LANGUAGES``; this adds the *enabled* selector rows with native labels and a
current-selection flag. The selector only surfaces when i18n is enabled and more
than one locale is offered, so an English-only deployment shows nothing.
"""
from __future__ import annotations

from django.conf import settings

from .config import anon_can_select, available_locales, is_i18n_enabled


def selector(request) -> dict:
    if not is_i18n_enabled():
        return {"i18n_locales": [], "i18n_show_selector": False}

    # Respect the "anonymous users may pick a language" policy toggle.
    if not getattr(getattr(request, "user", None), "is_authenticated", False) and not anon_can_select():
        return {"i18n_locales": [], "i18n_show_selector": False}

    current = getattr(request, "LANGUAGE_CODE", settings.LANGUAGE_CODE)
    locales = available_locales()
    for row in locales:
        row["current"] = row["code"] == current
    return {
        "i18n_locales": locales,
        "i18n_show_selector": len(locales) > 1,
        "i18n_current": current,
    }
