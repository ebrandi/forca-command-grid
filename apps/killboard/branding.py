"""KB-38 — corp branding/theming for a self-hosted killboard (WS-D5).

A corp's own board should feel like *theirs*, not a generic install. This is a small,
optional overlay stored in one ``AppSetting`` row (``killboard.branding``): a display-name
override, a logo URL, an accent colour and a footer tagline. Every field is optional and
falls back to today's behaviour — an unconfigured board looks exactly as it did before.

Deliberately **no file upload in v1**: the logo is a URL (an ``https://`` address or a
site-relative ``/static/`` path), which avoids a media-upload / storage / malware-scan
surface on the setup path. The accent colour is validated to a hex literal and applied via
inline styles on a contained set of killboard chrome (the list hero border/badge), because
the prebuilt Tailwind CSS is not recompiled on deploy — a new utility class would silently
do nothing, so themed colour must be inline.
"""
from __future__ import annotations

import re

_SETTING_KEY = "killboard.branding"
_CACHE_KEY = "killboard:branding:v1"
_CACHE_TTL = 600

# #rgb or #rrggbb, case-insensitive. Nothing else is accepted — the value goes straight into
# an inline ``style`` attribute, so it must be a colour literal and never arbitrary CSS.
_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

MAX_NAME = 60
MAX_TAGLINE = 160
MAX_LOGO = 300


def defaults() -> dict:
    return {"display_name": "", "logo_url": "", "accent_color": "", "footer_tagline": ""}


def get_branding() -> dict:
    """The stored branding overlay (cached). Missing/invalid fields come back empty."""
    from django.core.cache import cache

    cached = cache.get(_CACHE_KEY)
    if cached is None:
        from apps.admin_audit.models import AppSetting

        stored = AppSetting.get(_SETTING_KEY, {}) or {}
        cached = defaults()
        for key in cached:
            value = stored.get(key)
            if isinstance(value, str):
                cached[key] = value
        cache.set(_CACHE_KEY, cached, _CACHE_TTL)
    return dict(cached)


def display_name(fallback: str = "") -> str:
    """The corp display-name override, or ``fallback`` (the home-corp name) when unset."""
    return get_branding().get("display_name") or fallback


def accent_color() -> str:
    """The configured accent hex, or "" — callers gate their inline style on truthiness."""
    return get_branding().get("accent_color") or ""


def validate(data: dict) -> tuple[dict, list[str]]:
    """Clean a branding form submission. Returns ``(clean, errors)``.

    Each field is independently validated; an invalid one yields an error and is dropped
    from ``clean`` (so a bad accent never reaches an inline style, and current behaviour is
    preserved for that field). An all-blank submission is valid and clears the overlay.
    """
    from django.utils.translation import gettext as _

    clean = defaults()
    errors: list[str] = []

    name = (data.get("display_name") or "").strip()
    if len(name) > MAX_NAME:
        errors.append(_("Display name is too long (max %(n)d characters).") % {"n": MAX_NAME})
    else:
        clean["display_name"] = name

    tagline = (data.get("footer_tagline") or "").strip()
    if len(tagline) > MAX_TAGLINE:
        errors.append(_("Footer tagline is too long (max %(n)d characters).") % {"n": MAX_TAGLINE})
    else:
        clean["footer_tagline"] = tagline

    accent = (data.get("accent_color") or "").strip()
    if accent and not _HEX_RE.match(accent):
        errors.append(_("Accent colour must be a hex value like #c8a24b."))
    else:
        clean["accent_color"] = accent

    logo = (data.get("logo_url") or "").strip()
    if logo and not _valid_logo(logo):
        errors.append(_("Logo must be an https:// URL or a /static/ path."))
    elif len(logo) > MAX_LOGO:
        errors.append(_("Logo URL is too long."))
    else:
        clean["logo_url"] = logo

    return clean, errors


def _valid_logo(url: str) -> bool:
    """An ``https://`` URL or a site-relative ``/static``/``/media`` path — never ``javascript:``."""
    if url.startswith(("https://",)):
        return len(url) <= MAX_LOGO
    return url.startswith(("/static/", "/media/")) and len(url) <= MAX_LOGO


def set_branding(data: dict, *, user=None) -> tuple[dict, list[str]]:
    """Validate + persist branding and bust the cache. Returns ``(clean, errors)``.

    On any validation error nothing is written — the operator fixes the field and resubmits,
    rather than getting a partially-applied theme.
    """
    from django.core.cache import cache

    from apps.admin_audit.models import AppSetting

    clean, errors = validate(data)
    if errors:
        return clean, errors
    AppSetting.objects.update_or_create(
        key=_SETTING_KEY, defaults={"value": clean, "updated_by": user},
    )
    cache.delete(_CACHE_KEY)
    return clean, errors
