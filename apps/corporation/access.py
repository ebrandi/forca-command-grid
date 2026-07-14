"""Shared access logic for the corp's alliance-facing services.

The alliance services (logistics / buyback / store) are offered to the corp's
own alliance *and* to any extra access registered on the Access-governance console
page — partner alliances (:class:`PartnerAlliance`) and friendly corporations
(:class:`FriendlyCorporation`). Centralised here so every service and the nav agree
on exactly who counts. ``is_service_alliance_pilot`` is the single chokepoint (its
name is kept for its callers even though it now also covers friendly corps).
"""
from __future__ import annotations

from django.conf import settings
from django.core.cache import cache
from django.utils.translation import gettext_lazy as _

# These three values are read on (nearly) every page render via the ``roles`` context
# processor, but change only when leadership edits access governance or the home corp's
# affiliation is re-synced. Cache the small global sets/name in Redis so the hot path
# doesn't re-query them each request; invalidate explicitly on the governance writes
# (see ``invalidate_access_cache``), with a short TTL as the backstop for the rare
# home-affiliation change picked up by the corp sync.
_HOME_NAME_KEY = "access:home_corp_name:v1"
_ALLIANCE_IDS_KEY = "access:alliance_ids:v1"
_CORP_IDS_KEY = "access:corp_ids:v1"
_IDS_TTL = 300
_NAME_TTL = 3600


def service_corp_ids() -> set[int]:
    """Corporation ids whose pilots may use the alliance services (friendly corps)."""
    cached = cache.get(_CORP_IDS_KEY)
    if cached is not None:
        return set(cached)
    from .models import FriendlyCorporation

    ids: set[int] = set(
        FriendlyCorporation.objects.filter(active=True).values_list("corporation_id", flat=True)
    )
    ids.discard(0)
    cache.set(_CORP_IDS_KEY, list(ids), _IDS_TTL)
    return ids


def service_alliance_ids() -> set[int]:
    """Alliance ids whose pilots may use the alliance services.

    The corp's own alliance (from the home corporation) plus every *active*
    admin-registered partner alliance. Empty when neither is known.
    """
    cached = cache.get(_ALLIANCE_IDS_KEY)
    if cached is not None:
        return set(cached)
    from .models import EveCorporation, PartnerAlliance

    ids: set[int] = set(
        PartnerAlliance.objects.filter(active=True).values_list("alliance_id", flat=True)
    )
    home = (
        EveCorporation.objects.filter(corporation_id=getattr(settings, "FORCA_HOME_CORP_ID", 0))
        .values_list("alliance_id", flat=True)
        .first()
    )
    if home:
        ids.add(home)
    ids.discard(0)
    cache.set(_ALLIANCE_IDS_KEY, list(ids), _IDS_TTL)
    return ids


def home_corp_name() -> str:
    """Display name of the corporation that owns the app (the one customers make
    their in-game courier contracts to). Prefers the resolved EveCorporation name,
    falling back to the configured branding name."""
    cached = cache.get(_HOME_NAME_KEY)
    if cached is not None:
        return cached
    from .models import EveCorporation

    name = (
        EveCorporation.objects.filter(corporation_id=getattr(settings, "FORCA_HOME_CORP_ID", 0))
        .values_list("name", flat=True)
        .first()
    )
    # Lazy: the value is cached for an hour and re-resolved per viewer on read.
    result = name or getattr(settings, "FORCA_CORP_NAME", "") or _("our corporation")
    cache.set(_HOME_NAME_KEY, result, _NAME_TTL)
    return result


def invalidate_access_cache() -> None:
    """Drop the cached access sets + home-corp name. Call on any PartnerAlliance /
    FriendlyCorporation write and whenever the home corp's affiliation/name is refreshed."""
    cache.delete_many([_HOME_NAME_KEY, _ALLIANCE_IDS_KEY, _CORP_IDS_KEY])


def is_service_alliance_pilot(user) -> bool:
    """True if any of ``user``'s characters belongs to an allowed alliance OR a
    registered friendly corporation. (Name kept for its callers; scope now also
    covers friendly corps.)

    The expensive part — resolving the allowed alliance/corp id sets — is served from the
    cached ``service_alliance_ids`` / ``service_corp_ids`` above (the repeated per-request
    queries the audit flagged). The result is deliberately NOT memoised on the user
    instance: this gates access, and a revoked partner/friendly must take effect on the very
    next check even when the same user object is reused (see tests/test_partner_alliance.py,
    tests/test_friendly_corp.py). The remaining cost is one or two cheap character EXISTS."""
    if not getattr(user, "is_authenticated", False):
        return False
    alliance_ids = service_alliance_ids()
    if alliance_ids and user.characters.filter(alliance_id__in=alliance_ids).exists():
        return True
    corp_ids = service_corp_ids()
    if corp_ids and user.characters.filter(corporation_id__in=corp_ids).exists():
        return True
    return False
