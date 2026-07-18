"""KB-21 — reconstruct an EVE-style per-slot fit from a killmail's items.

A killmail lists every item that was aboard, each tagged with its ESI inventory ``flag``
(the slot it sat in). We bucket those items into the in-game fitting layout
(high/med/low/rig/subsystem/drone/implant/cargo/other) and — when we know the hull's slot
counts — pad each slot row out to the hull's capacity so **empty** slots render too, exactly
like the fitting window.

This is our own renderer: everything is derived server-side from data we already store, with
no external component and no calls off-site. We *improve* on a generic fit viewer with
killmail-only context — destroyed-vs-dropped per module, per-item ISK, and (for permitted
viewers) off-doctrine markers.
"""
from __future__ import annotations

from decimal import Decimal

from django.utils.translation import gettext_lazy as _

from apps.sde.models import SdeType


def slot_bucket(flag: int) -> str:
    """Map an ESI inventory ``flag`` to a fitting-window bucket.

    Ranges follow EVE's invFlags: Lo 11-18 · Med 19-26 · Hi 27-34 · Rig 92-99 ·
    Subsystem 125-132 · DroneBay 87 · Cargo 5 · Implant 89. Anything else (fuel bay,
    fleet hangar, ore hold, …) falls to ``other``.
    """
    if 27 <= flag <= 34:
        return "high"
    if 19 <= flag <= 26:
        return "med"
    if 11 <= flag <= 18:
        return "low"
    if 92 <= flag <= 99:
        return "rig"
    if 125 <= flag <= 132:
        return "subsystem"
    if flag == 87:
        return "drone"
    if flag == 89:
        return "implant"
    if flag == 5:
        return "cargo"
    return "other"


# (bucket key, label, hull-capacity field on SdeType or None). Order = fitting-window order.
# Only the four module racks have a capacity we can pad to empty slots; the rest render
# occupied-only (drones/cargo/holds have no fixed "slot count", and T3 losses always carry
# their subsystems).
_SLOT_META: list[tuple[str, object, str | None]] = [
    ("high", _("High slots"), "hi_slots"),
    ("med", _("Mid slots"), "med_slots"),
    ("low", _("Low slots"), "low_slots"),
    ("rig", _("Rigs"), "rig_slots"),
    ("subsystem", _("Subsystems"), None),
    ("drone", _("Drone bay"), None),
    ("implant", _("Implants"), None),
    ("cargo", _("Cargo hold"), None),
    ("other", _("Other"), None),
]

_CAPACITY_FIELDS = ("hi_slots", "med_slots", "low_slots", "rig_slots")


def _empty_row() -> dict:
    return {
        "type_id": None, "name": "", "flag": None, "destroyed": 0, "dropped": 0,
        "qty": 0, "value": Decimal("0"), "off_doctrine": False, "empty": True,
    }


def build_fit(killmail, deviation=None) -> dict:
    """Bucket a killmail's items into fitting-window sections.

    ``deviation`` (a :class:`FitDeviation` or ``None``) drives the off-doctrine markers.
    The caller MUST pass ``None`` for viewers not allowed to see deviations — the detail
    view already gates it to the loss owner + officers — so peers never get the overlay.
    """
    items = list(killmail.items.all())
    names = dict(
        SdeType.objects.filter(type_id__in={it.item_type_id for it in items})
        .values_list("type_id", "name")
    )
    off_doctrine: set[int] = set()
    if deviation is not None:
        off_doctrine = {int(e["type_id"]) for e in (deviation.extra or [])}

    hull = (
        SdeType.objects.filter(type_id=killmail.victim_ship_type_id)
        .values(*_CAPACITY_FIELDS)
        .first()
        or {}
    )
    has_slot_data = any(hull.get(f) is not None for f in _CAPACITY_FIELDS)

    buckets: dict[str, list[dict]] = {key: [] for key, _label, _cap in _SLOT_META}
    for it in items:
        qty = (it.quantity_destroyed or 0) + (it.quantity_dropped or 0)
        buckets[slot_bucket(it.flag)].append({
            "type_id": it.item_type_id,
            "name": names.get(it.item_type_id) or f"Type {it.item_type_id}",
            "flag": it.flag,
            "destroyed": it.quantity_destroyed or 0,
            "dropped": it.quantity_dropped or 0,
            "qty": qty,
            "value": (it.unit_value or Decimal("0")) * qty,
            "off_doctrine": it.item_type_id in off_doctrine,
            "empty": False,
        })

    sections = []
    for key, label, cap_field in _SLOT_META:
        rows = sorted(buckets[key], key=lambda r: (r["flag"], r["name"]))
        capacity = hull.get(cap_field) if cap_field else None
        # A module and its loaded charge share a slot flag → one slot, two rows. Count
        # occupied slots as distinct flags so the "filled/capacity" header is truthful.
        filled = len({r["flag"] for r in rows})
        count = len(rows)
        if capacity is not None and capacity > filled:
            rows = rows + [_empty_row() for _ in range(capacity - filled)]
        if not rows and not capacity:
            continue
        sections.append({
            "key": key,
            "label": label,
            "capacity": capacity,
            "filled": filled,
            "count": count,
            "items": rows,
            "value": sum((r["value"] for r in rows), Decimal("0")),
        })
    return {"sections": sections, "has_slot_data": has_slot_data}


def esi_fitting(killmail) -> dict:
    """The loss as an ESI-shaped fitting dict (``ship_type_id`` + flagged ``items``).

    Quantities are summed per (type_id, slot) across destroyed + dropped. Mirrors the ESI
    ``fittings`` payload so a member can round-trip the fit through their own tooling — all
    generated locally, nothing leaves the box.
    """
    agg: dict[tuple[int, int], int] = {}
    order: list[tuple[int, int]] = []
    for it in killmail.items.all():
        qty = (it.quantity_destroyed or 0) + (it.quantity_dropped or 0)
        if qty <= 0:
            continue
        key = (it.item_type_id, it.flag)
        if key not in agg:
            order.append(key)
        agg[key] = agg.get(key, 0) + qty
    ship_name = (
        SdeType.objects.filter(type_id=killmail.victim_ship_type_id)
        .values_list("name", flat=True).first()
        or f"Type {killmail.victim_ship_type_id}"
    )
    return {
        "name": f"{ship_name} - Killmail {killmail.killmail_id}",
        "description": "",
        "ship_type_id": killmail.victim_ship_type_id,
        "items": [
            {"flag": flag, "quantity": agg[(tid, flag)], "type_id": tid}
            for (tid, flag) in order
        ],
    }
