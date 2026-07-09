"""Template helpers for EVE imagery, names, and data formatting.

Image URLs come straight from CCP's image server (images.evetech.net). Name
lookups resolve against the SDE and are cached so repeated use across a page is
cheap.
"""
from __future__ import annotations

from django import template
from django.core.cache import cache

from apps.sde.models import SdeRegion, SdeSolarSystem, SdeType

register = template.Library()


@register.simple_tag
def eve_name(entity_id):
    """Resolved name for a character/corp/alliance id (from EveName), or the id.

    EveName is populated asynchronously (background name resolution), so the
    '#id' fallback is deliberately NOT cached — otherwise an id rendered before
    its name lands (e.g. the home corp right after a fresh killmail import)
    would stick at '#id' for the cache lifetime. Only real names are cached.
    """
    if not entity_id:
        return ""
    from apps.corporation.models import EveName

    key = f"eve:name:{entity_id}"
    if key in _LOCAL:  # resolved name memoised for this process
        return _LOCAL[key]
    cached = cache.get(key)
    if cached:
        _LOCAL[key] = cached
        return cached
    name = EveName.objects.filter(entity_id=entity_id).values_list("name", flat=True).first()
    if name:
        cache.set(key, name, 86400)
        if len(_LOCAL) < _LOCAL_CAP:
            _LOCAL[key] = name
        return name
    return f"#{entity_id}"  # NOT memoised — re-resolves once the async name lands

def _img_base() -> str:
    """The image origin/prefix (CCP's server in dev, the same-origin /eveimg
    proxy-cache in prod). Read per call so settings overrides apply in tests."""
    from django.conf import settings

    return getattr(settings, "EVE_IMAGE_BASE_URL", "https://images.evetech.net").rstrip("/")


# The image server only serves these discrete sizes; anything else 404s. We snap a
# requested size up to the smallest valid size that is at least as large (so we
# never downgrade sharpness), capped at the maximum.
_VALID_SIZES = (32, 64, 128, 256, 512, 1024)


def _snap_size(size) -> int:
    try:
        size = int(size)
    except (TypeError, ValueError):
        return 64
    for valid in _VALID_SIZES:
        if size <= valid:
            return valid
    return _VALID_SIZES[-1]


# --- imagery ---------------------------------------------------------------
@register.simple_tag
def eve_portrait(character_id, size=64):
    return f"{_img_base()}/characters/{character_id}/portrait?size={_snap_size(size)}" if character_id else ""


@register.simple_tag
def eve_corp_logo(corporation_id, size=64):
    return f"{_img_base()}/corporations/{corporation_id}/logo?size={_snap_size(size)}" if corporation_id else ""


@register.simple_tag
def eve_alliance_logo(alliance_id, size=64):
    return f"{_img_base()}/alliances/{alliance_id}/logo?size={_snap_size(size)}" if alliance_id else ""


@register.simple_tag
def eve_img_base():
    """The image origin/prefix for client-side (Alpine) image URLs.

    Lets JS build icon URLs against the same-origin ``/eveimg`` mirror in prod
    instead of hardcoding CCP's server, which the ``img-src 'self'`` CSP blocks.
    """
    return _img_base()


@register.simple_tag
def eve_type_icon(type_id, size=64):
    return f"{_img_base()}/types/{type_id}/icon?size={_snap_size(size)}" if type_id else ""


@register.simple_tag
def eve_type_render(type_id, size=512):
    return f"{_img_base()}/types/{type_id}/render?size={_snap_size(size)}" if type_id else ""


# --- names (cached) --------------------------------------------------------
# Process-local memo in FRONT of Redis. SDE names/security are static for the life
# of a process (a re-import is a deploy, which restarts web and clears this), so a
# list page rendering hundreds of rows resolves each id once from memory instead of
# making one Redis round-trip per cell — the dominant cost on row-heavy pages.
_LOCAL: dict[str, object] = {}
_LOCAL_CAP = 60000  # ~ the whole SDE; a hard ceiling so the dict can never run away


def _cached(key, fetch):
    if key in _LOCAL:
        return _LOCAL[key]
    val = cache.get(key)
    if val is None:
        val = fetch()
        cache.set(key, val, 86400)
    if len(_LOCAL) < _LOCAL_CAP:
        _LOCAL[key] = val
    return val


@register.simple_tag
def type_name(type_id):
    if not type_id:
        return ""
    return _cached(
        f"sde:t:{type_id}",
        lambda: SdeType.objects.filter(type_id=type_id).values_list("name", flat=True).first()
        or f"Type {type_id}",
    )


@register.simple_tag
def system_name(system_id):
    if not system_id:
        return ""
    return _cached(
        f"sde:s:{system_id}",
        lambda: SdeSolarSystem.objects.filter(system_id=system_id)
        .values_list("name", flat=True)
        .first()
        or f"System {system_id}",
    )


@register.simple_tag
def region_name(region_id):
    if not region_id:
        return ""
    return _cached(
        f"sde:r:{region_id}",
        lambda: SdeRegion.objects.filter(region_id=region_id).values_list("name", flat=True).first()
        or "",
    )


@register.simple_tag
def system_security(system_id):
    """Rounded security status of a system (e.g. 0.5), or None."""
    if not system_id:
        return None
    return _cached(
        f"sde:sec:{system_id}",
        lambda: round(
            SdeSolarSystem.objects.filter(system_id=system_id)
            .values_list("security", flat=True)
            .first()
            or 0.0,
            1,
        ),
    )


# --- formatting ------------------------------------------------------------
@register.filter
def isk(value):
    """Compact ISK formatting: 1.2B, 340M, 12.5k, 540."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "0"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(v) >= div:
            return f"{v / div:.2f}{unit}"
    return f"{v:.0f}"


@register.filter
def effpct(value):
    """ISK-efficiency percentage for display — the single, consistent formatter
    used everywhere the killboard shows "efficiency".

    Efficiency is ISK destroyed / (ISK destroyed + ISK lost). Naïve rounding
    (``floatformat``) pushes a value like 99.56% up to a misleading **100%** for a
    pilot who has clearly lost ships. This rounds to one decimal but **never rounds
    up to 100** — only a genuine 100.0% (zero ISK lost) ever shows "100". So a pilot
    with any ISK lost can never read as a perfect 100%.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "0"
    if v >= 100:
        return "100"          # truly lossless (no ISK lost)
    if v <= 0:
        return "0"
    r = round(v, 1)
    if r >= 100:              # would round up to 100, but the pilot has losses
        r = 99.9
    return f"{r:.1f}"


@register.filter
def sp(value):
    """Compact skill-point formatting."""
    return isk(value)


@register.filter
def duration(seconds):
    """Human training time: ``3d 4h``, ``5h 20m``, ``12m``."""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "—"
    if s <= 0:
        return "0m"
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    return " ".join(parts) or "<1m"


@register.filter
def sec_class(security):
    """Tailwind text colour for an EVE security status, in the four bands pilots
    read the map by: cyan 1.0–0.8, yellow 0.7–0.5, orange 0.4–0.1, red 0.0 and
    below. Thresholds sit on the .05 midpoints so a value rounds into the same
    band it displays as (e.g. a true 0.45 shows as 0.5 → yellow)."""
    try:
        s = float(security)
    except (TypeError, ValueError):
        return "text-faint"
    if s >= 0.75:
        return "text-sechi"
    if s >= 0.45:
        return "text-secmid"
    if s >= 0.05:
        return "text-seclo"
    return "text-secnull"
