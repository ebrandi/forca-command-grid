"""Shared helpers for the navigation tools: incursions and avoidance parsing."""
from __future__ import annotations

from django.core.cache import cache

from apps.sde.models import SdeRegion, SdeSolarSystem
from core.esi.client import ESIClient, ESIError

_INCURSION_KEY = "nav:incursions"
_INCURSION_TTL = 900  # 15 min — incursions move slowly


def incursion_systems() -> set[int]:
    """Solar systems currently affected by an incursion (public ESI, cached)."""
    cached = cache.get(_INCURSION_KEY)
    if cached is None:
        ids: list[int] = []
        try:
            resp = ESIClient().get("/incursions/")
            for inc in resp.data or []:
                ids.extend(inc.get("infested_solar_systems", []) or [])
        except ESIError:
            ids = []
        cached = ids
        cache.set(_INCURSION_KEY, cached, _INCURSION_TTL)
    return set(cached)


# Cap how many comma/newline-separated names we resolve per request. Each token
# becomes its own DB lookup, and a resolved waypoint additionally becomes a route
# leg (an outbound ESI /route call). The planners are public/unauthenticated, so
# an unbounded list (``?waypoints=Jita,Jita,…×50k``) is a query/ESI amplification
# DoS — bound it to a count no real route needs.
_MAX_TOKENS = 64


def _split(text: str) -> list[str]:
    tokens = [t.strip() for t in (text or "").replace("\n", ",").split(",") if t.strip()]
    return tokens[:_MAX_TOKENS]


def ansiblex_connections() -> list[dict]:
    """Active Ansiblex bridges as ESI ``/route`` connections (both directions).

    ESI's body-based /route expects each connection as an object ``{"from","to"}``
    (not a two-element array).
    """
    from .models import AnsiblexBridge

    out: list[dict] = []
    for a, b in AnsiblexBridge.objects.filter(active=True).values_list(
        "from_system_id", "to_system_id"
    ):
        out.append({"from": a, "to": b})
        out.append({"from": b, "to": a})
    return out


def resolve_waypoints(text: str) -> tuple[list, list[str]]:
    """Resolve an ordered, comma-separated list of system names to SdeSolarSystem
    objects (route is forced through them, in order). Returns (systems, unresolved)."""
    systems = []
    unresolved: list[str] = []
    for name in _split(text):
        s = (
            SdeSolarSystem.objects.filter(name__iexact=name).first()
            or SdeSolarSystem.objects.filter(name__istartswith=name).order_by("name").first()
        )
        if s:
            systems.append(s)
        else:
            unresolved.append(name)
    return systems, unresolved


def resolve_avoidance(systems_text: str, regions_text: str) -> tuple[set[int], list[str]]:
    """Resolve avoid-system and avoid-region names to a set of solar-system ids.

    Returns ``(avoid_system_ids, unresolved_names)`` so the UI can flag typos.
    A region name expands to every solar system in that region.
    """
    avoid: set[int] = set()
    unresolved: list[str] = []
    for name in _split(systems_text):
        sid = (
            SdeSolarSystem.objects.filter(name__iexact=name)
            .values_list("system_id", flat=True).first()
        )
        if sid:
            avoid.add(sid)
        else:
            unresolved.append(name)
    for name in _split(regions_text):
        rid = (
            SdeRegion.objects.filter(name__iexact=name)
            .values_list("region_id", flat=True).first()
        )
        if rid:
            avoid.update(
                SdeSolarSystem.objects.filter(region_id=rid).values_list("system_id", flat=True)
            )
        else:
            unresolved.append(name)
    return avoid, unresolved
