"""Freight-costing primitives — the one authority for packaged volume and the
per-hull jump-freight cost.

Promoted here from :mod:`apps.store.forecast` (where they were module-private yet
imported cross-app) so the forecaster, the MRP import lane and the P6 freight
pipeline all price a leg the same way. The logistics app owns the rate card,
pricing and routing these build on, so this is their natural home.

Volume always comes from EVE Ref's repackaged volume (``SdeType.packaged_volume``)
first, falling back to a per-class approximation — NEVER the assembled volume (a
Rifter is 27,289 m³ assembled vs 2,500 m³ packaged). Pure reads; writes nothing.
Heavy imports stay lazy (inside functions) so importing this module is cheap and
load-order-safe.
"""
from __future__ import annotations

from decimal import Decimal

JITA_SYSTEM_ID = 30000142

# EVE's fixed repackaged ship volumes (m³) by broad hull class — used to amortise a
# jump-freighter load across how many hulls fit in it, and as the packaged-volume
# fallback when EVE Ref reference-data hasn't been loaded for a type.
PACKAGED_VOL = {
    "Frigate": 2_500, "Destroyer": 5_000, "Cruiser": 10_000, "Battlecruiser": 15_000,
    "Battleship": 50_000, "Industrial": 20_000, "Capital": 1_300_000,
    "Freighter": 1_300_000, "Other": 10_000,
}


def _staging_hops(staging_system_id: int) -> int:
    """Cyno hops from Jita to the staging system at a JDC V jump-freighter range."""
    if not staging_system_id:
        return 0
    from apps.logistics.jumps import SHIPS_BY_KEY, effective_range
    from apps.logistics.routing import jf_route_facts

    rng = effective_range(SHIPS_BY_KEY["jf"]["range"], 5)
    try:
        return jf_route_facts(JITA_SYSTEM_ID, staging_system_id, rng)["jumps"]
    except Exception:  # noqa: BLE001 - no route / no coords → treat as no jump legs
        return 0


def _freight_unit(card, hops: int, hull_class: str, unit_value: Decimal,
                  *, packaged_vol: float | None = None) -> Decimal:
    """Per-hull jump-freight cost Jita→staging, off the corp's own JF rate card.

    Uses the real repackaged volume when we have it (EVE Ref reference-data), falling
    back to a per-class approximation otherwise.
    """
    if hops <= 0:
        return Decimal("0")
    from apps.logistics.models import ShipClass
    from apps.logistics.pricing import caps_for, quote

    vol = packaged_vol or PACKAGED_VOL.get(hull_class, 10_000)
    max_m3, max_coll = caps_for(card, ShipClass.JF)
    by_vol = int(max_m3 // vol) if vol else 1
    by_coll = int(max_coll // unit_value) if unit_value > 0 else by_vol
    units = max(1, min(by_vol or 1, by_coll or 1))
    q = quote(
        card, ship_class=ShipClass.JF, jumps=hops, jump_hops=hops,
        volume_m3=vol * units, collateral=float(unit_value) * units, sec_band="nullsec",
    )
    if not q.ok:
        return Decimal("0")
    return (Decimal(q.reward) / units).quantize(Decimal("0.01"))


def packaged_volume(type_id: int) -> float:
    """Packaged m³ for a type — NEVER the assembled volume.

    EVE Ref reference-data (``SdeType.packaged_volume``) first, then the per-class
    ``PACKAGED_VOL`` approximation, then the assembled volume as a last resort.
    """
    from apps.doctrines.hulls import hull_class_for_group
    from apps.sde.models import SdeType

    row = SdeType.objects.filter(type_id=type_id).values(
        "packaged_volume", "volume", "group_id"
    ).first()
    if not row:
        return 0.0
    if row["packaged_volume"]:
        return float(row["packaged_volume"])
    hull_class = hull_class_for_group(row["group_id"]) if row["group_id"] else "Other"
    if hull_class in PACKAGED_VOL:
        return float(PACKAGED_VOL[hull_class])
    return float(row["volume"] or 0.0)
