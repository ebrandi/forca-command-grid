"""Template tags for the Combat Signatures nav plug-in.

One tag, :func:`signatures_enabled`, reports the leadership master switch
(``CombatSignatureSettings.enabled``) so ``_nav.html`` can show the "Signatures" link only when the
feature is armed — the killboard feature flag is already checked by the surrounding
``{% if features.killboard %}``. The value is cached for 600s (matching ``core.features``'s caching
style) because the nav renders on every authenticated page and the master switch changes rarely
(via the admin console); a leadership toggle then takes at most ten minutes to surface in the nav.
"""
from __future__ import annotations

from django import template
from django.core.cache import cache

register = template.Library()

_CACHE_KEY = "kb:sig:nav_enabled"
_CACHE_TTL = 600


@register.simple_tag
def signatures_enabled() -> bool:
    """True when Combat Signatures is switched on for the corporation (cached 600s)."""
    cached = cache.get(_CACHE_KEY)
    if cached is None:
        from apps.killboard.models import CombatSignatureSettings

        cached = bool(CombatSignatureSettings.load().enabled)
        cache.set(_CACHE_KEY, cached, _CACHE_TTL)
    return cached
