"""Buyback service helpers: the active config and access control."""
from __future__ import annotations

from django.core.cache import cache

from .models import Audience, BuybackConfig

_AUDIENCE_CACHE_KEY = "buyback:audience"


def active_config() -> BuybackConfig:
    """The config every appraisal uses. Seeds the default the first time."""
    cfg = BuybackConfig.objects.filter(is_active=True).order_by("-updated_at").first()
    if cfg is None:
        cfg = BuybackConfig.objects.create(name="Standard", is_active=True)
    return cfg


def current_audience() -> str:
    """Service audience (cached; read-only so it's safe in a context processor)."""
    cached = cache.get(_AUDIENCE_CACHE_KEY)
    if cached is None:
        cached = (
            BuybackConfig.objects.filter(is_active=True)
            .order_by("-updated_at")
            .values_list("audience", flat=True)
            .first()
            or BuybackConfig._meta.get_field("audience").default
        )
        cache.set(_AUDIENCE_CACHE_KEY, cached, 300)
    return cached


def invalidate_audience_cache() -> None:
    cache.delete(_AUDIENCE_CACHE_KEY)


def _audience_allows(user, audience: str) -> bool:
    """Whether ``user`` is inside a given audience band (public/corp/alliance/disabled).
    Shared by the member buyback (``can_access``) and the guaranteed buyback (4.20)."""
    if audience == Audience.PUBLIC:
        return True
    if audience == Audience.DISABLED:
        return False
    from core import rbac

    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False) or rbac.has_role(user, rbac.ROLE_MEMBER):
        return True
    if audience == Audience.ALLIANCE:
        from apps.corporation.access import is_service_alliance_pilot

        return is_service_alliance_pilot(user)
    return False


def can_access(user) -> bool:
    """Whether ``user`` may use the buyback service under the current audience."""
    return _audience_allows(user, current_audience())
