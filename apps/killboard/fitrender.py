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

import math
from decimal import Decimal

from django.utils.translation import gettext_lazy as _

from apps.sde.models import SdeType

_CHARGE_CATEGORY = 8  # SDE category for ammo/charges — loaded into a module, not its own slot


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
        "category_id": None,
    }


def build_fit(killmail, deviation=None) -> dict:
    """Bucket a killmail's items into fitting-window sections.

    ``deviation`` (a :class:`FitDeviation` or ``None``) drives the off-doctrine markers.
    The caller MUST pass ``None`` for viewers not allowed to see deviations — the detail
    view already gates it to the loss owner + officers — so peers never get the overlay.
    """
    items = list(killmail.items.all())
    meta = {
        tid: (name, cat)
        for tid, name, cat in SdeType.objects.filter(
            type_id__in={it.item_type_id for it in items}
        ).values_list("type_id", "name", "group__category_id")
    }
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
        name, category_id = meta.get(it.item_type_id, (None, None))
        buckets[slot_bucket(it.flag)].append({
            "type_id": it.item_type_id,
            "name": name or f"Type {it.item_type_id}",
            "flag": it.flag,
            "destroyed": it.quantity_destroyed or 0,
            "dropped": it.quantity_dropped or 0,
            "qty": qty,
            "value": (it.unit_value or Decimal("0")) * qty,
            "off_doctrine": it.item_type_id in off_doctrine,
            "empty": False,
            "category_id": category_id,
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


# Radial layout of the in-game fitting window: each module rack sits on an arc around the
# centred ship render. (rack key → (centre angle clockwise-from-top in degrees, ring radius %)).
# High=top, Mid=left, Low=bottom, Rigs=right, Subsystems=inner-bottom — the recognisable
# EVE fitting-screen arrangement, drawn entirely in-house.
_RACK_GEOMETRY: dict[str, tuple[float, float]] = {
    "high": (0.0, 39.0),
    "med": (270.0, 39.0),
    "low": (180.0, 39.0),
    "rig": (90.0, 34.0),
    "subsystem": (180.0, 22.0),
}
_SLOT_STEP_DEG = 19.0   # angular gap between adjacent slots on a rack's arc
_SLOT_SPREAD_MAX = 150.0  # cap so a full 8-slot rack doesn't wrap past its quadrant


def _slot_positions(center_deg: float, n: int, radius: float) -> list[tuple[float, float]]:
    """``n`` evenly-spaced ``(x%, y%)`` points on an arc centred at ``center_deg`` (a rack)."""
    if n <= 0:
        return []
    step = _SLOT_STEP_DEG if n == 1 else min(_SLOT_STEP_DEG, _SLOT_SPREAD_MAX / (n - 1))
    start = center_deg - step * (n - 1) / 2
    pts = []
    for i in range(n):
        a = math.radians(start + step * i)
        pts.append((round(50.0 + radius * math.sin(a), 2), round(50.0 - radius * math.cos(a), 2)))
    return pts


def build_fit_wheel(killmail, deviation=None) -> dict:
    """The fit as a radial fitting-window layout (KB-21b): the fitted module racks placed on
    arcs around the hull, plus the leftover holds (drones/cargo/implants) as ``extras``.

    Builds on :func:`build_fit` (bucketing, empty-slot padding, off-doctrine markers). A
    loaded charge shares its module's slot flag, so it is folded into that module's tooltip
    (``charges``) instead of taking a slot of its own.
    """
    fit = build_fit(killmail, deviation)
    sections = {s["key"]: s for s in fit["sections"]}

    slots = []
    for key, (center, radius) in _RACK_GEOMETRY.items():
        sec = sections.get(key)
        if not sec:
            continue
        placed, charges_by_flag = [], {}
        for it in sec["items"]:
            if not it["empty"] and it.get("category_id") == _CHARGE_CATEGORY:
                charges_by_flag.setdefault(it["flag"], []).append(it["name"])
            else:
                placed.append(it)
        for (x, y), it in zip(_slot_positions(center, len(placed), radius), placed, strict=True):
            slots.append({
                **it, "x": x, "y": y, "rack": key,
                "charges": [] if it["empty"] else charges_by_flag.get(it["flag"], []),
            })

    extras = [s for s in fit["sections"] if s["key"] in ("drone", "implant", "cargo", "other")]
    return {
        "hull_type_id": killmail.victim_ship_type_id,
        "slots": slots,
        "extras": extras,
        "has_slot_data": fit["has_slot_data"],
    }


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
