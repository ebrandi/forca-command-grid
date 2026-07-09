"""Assemble the per-system info page from our own data.

Static facts (security, neighbours, gateways, NPC stations, recent corp kills) come
straight from the SDE + our killboard — no ESI, so this is fast and easy to test.
The view layers live ESI activity (traffic, kills, sovereignty, incursions) and the
cached celestial counts on top.
"""
from __future__ import annotations

from apps.sde.models import SdeRegion, SdeSolarSystem, SdeStation, SdeSystemJump, SdeType

from .maps import security_band, security_colour

# Representative asteroid-belt ores by security band. Belt contents are standard
# across New Eden by security class, so this is a useful reference rather than
# per-belt data (which isn't published). Shown as guidance, not a guarantee.
BELT_ORES = {
    "highsec": ["Veldspar", "Scordite", "Pyroxeres", "Plagioclase", "Omber", "Kernite"],
    "lowsec": ["Veldspar", "Scordite", "Pyroxeres", "Plagioclase", "Jaspet",
               "Hemorphite", "Hedbergite", "Gneiss"],
    "nullsec": ["Gneiss", "Dark Ochre", "Crokite", "Spodumain", "Bistot",
                "Arkonor", "Mercoxit"],
}


def _victim_names(char_ids: set[int], corp_ids: set[int]) -> dict[int, str]:
    try:
        from apps.corporation.models import EveName
    except Exception:  # pragma: no cover - corporation app always present
        return {}
    ids = {i for i in (char_ids | corp_ids) if i}
    if not ids:
        return {}
    return dict(EveName.objects.filter(entity_id__in=ids).values_list("entity_id", "name"))


def recent_kills(system_id: int, limit: int = 12) -> list[dict]:
    """Most recent killmails recorded in this system (our killboard)."""
    from apps.killboard.models import Killmail

    kms = list(
        Killmail.objects.filter(solar_system_id=system_id)
        .order_by("-killmail_time")[:limit]
        .values("killmail_id", "killmail_time", "victim_ship_type_id",
                "victim_character_id", "victim_corporation_id", "total_value", "is_npc")
    )
    if not kms:
        return []
    ship_ids = {k["victim_ship_type_id"] for k in kms}
    ship_names = dict(
        SdeType.objects.filter(type_id__in=ship_ids).values_list("type_id", "name")
    )
    names = _victim_names(
        {k["victim_character_id"] for k in kms},
        {k["victim_corporation_id"] for k in kms},
    )
    rows = []
    for k in kms:
        victim = names.get(k["victim_character_id"]) or names.get(k["victim_corporation_id"])
        rows.append({
            "killmail_id": k["killmail_id"],
            "time": k["killmail_time"],
            "ship": ship_names.get(k["victim_ship_type_id"], f"Type {k['victim_ship_type_id']}"),
            "ship_id": k["victim_ship_type_id"],
            "victim": victim or ("NPC" if k["is_npc"] else "—"),
            "value": k["total_value"],
        })
    return rows


def celestials(system_id: int) -> dict | None:
    """Named planets, each with its moons and asteroid belts (from the SDE).

    ``None`` when celestials haven't been loaded, so the page can fall back to live
    ESI counts (see import_sde_fuzzwork --celestials-only).
    """
    from apps.sde.models import SdeCelestial

    rows = list(SdeCelestial.objects.filter(system_id=system_id))
    if not rows:
        return None
    planet_rows = [c for c in rows if c.kind == SdeCelestial.Kind.PLANET]
    moons: dict[int, list[str]] = {}
    belts: dict[int, list[str]] = {}
    for c in rows:
        if c.kind == SdeCelestial.Kind.MOON:
            moons.setdefault(c.parent_planet_id, []).append(c.name)
        elif c.kind == SdeCelestial.Kind.BELT:
            belts.setdefault(c.parent_planet_id, []).append(c.name)
    planet_rows.sort(key=lambda p: (p.celestial_index or 0, p.name))
    planets = [
        {
            "name": p.name,
            "moons": sorted(moons.get(p.item_id, [])),
            "belts": sorted(belts.get(p.item_id, [])),
        }
        for p in planet_rows
    ]
    return {
        "planets": planets,
        "planet_count": len(planets),
        "moon_count": sum(len(p["moons"]) for p in planets),
        "belt_count": sum(len(p["belts"]) for p in planets),
    }


def system_facts(system_id: int) -> dict | None:
    """Static facts for a solar system (no ESI). ``None`` if the system is unknown."""
    sys = (
        SdeSolarSystem.objects.filter(system_id=system_id)
        .select_related("region", "constellation").first()
    )
    if not sys:
        return None

    band = security_band(sys.security)

    # Gate neighbours — flag the ones that lead out of this region (gateways).
    neigh_ids = list(
        SdeSystemJump.objects.filter(from_system_id=system_id)
        .values_list("to_system_id", flat=True)
    )
    neighbours, gateways = [], set()
    if neigh_ids:
        rows = SdeSolarSystem.objects.filter(system_id__in=neigh_ids).select_related("region")
        for n in sorted(rows, key=lambda r: r.name):
            external = n.region_id != sys.region_id
            if external and n.region:
                gateways.add(n.region.name)
            neighbours.append({
                "system_id": n.system_id, "name": n.name,
                "security": round(n.security, 1), "band": security_band(n.security),
                "colour": security_colour(n.security),
                "region": n.region.name if n.region else "",
                "external": external,
            })

    stations = list(
        SdeStation.objects.filter(system_id=system_id).order_by("name")
        .values("station_id", "name")
    )

    return {
        "system": {
            "id": sys.system_id, "name": sys.name,
            "security": round(sys.security, 1), "band": band,
            "colour": security_colour(sys.security),
            "region_id": sys.region_id,
            "region": sys.region.name if sys.region else "",
            "constellation": sys.constellation.name if sys.constellation else "",
        },
        "neighbours": neighbours,
        "gateways": sorted(gateways),
        "stations": stations,
        "ores": BELT_ORES.get(band, []),
        "recent_kills": recent_kills(system_id),
        "celestials": celestials(system_id),
    }


def resolve_region_name(region_id: int | None) -> str:
    if not region_id:
        return ""
    r = SdeRegion.objects.filter(region_id=region_id).first()
    return r.name if r else ""
