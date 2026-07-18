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


# zKillboard-exact fitting-wheel geometry: a fixed 398x398 box (rendered responsively via
# percentages) with the hull centred and each module at the hard-coded position for its EVE
# inventory flag — High across the top, Mid down the left, Low down the right, Rigs an inner
# arc at the bottom, Subsystems an inner arc at the top. Coordinates are the top-left of each
# icon inside the 398 box, from zKillboard's fitting_wheel component; the ring frame and empty
# sockets are drawn with our own CSS/SVG (no external assets).
_WHEEL_BOX = 398
_MOD_PX = 32   # module icon size
_CHG_PX = 24   # loaded-charge icon (nested inward from its module)

# EVE inventory flag -> module icon top-left (px in the 398 box).
_MOD_XY: dict[int, tuple[int, int]] = {
    27: (73, 60), 28: (102, 42), 29: (134, 27), 30: (169, 21),
    31: (203, 22), 32: (238, 30), 33: (270, 45), 34: (295, 64),            # high (top)
    19: (26, 140), 20: (24, 176), 21: (23, 212), 22: (30, 245),
    23: (46, 278), 24: (69, 304), 25: (100, 328), 26: (133, 342),          # mid (left)
    11: (344, 143), 12: (350, 178), 13: (349, 213), 14: (340, 246),
    15: (323, 277), 16: (300, 304), 17: (268, 324), 18: (234, 338),        # low (right)
    92: (148, 259), 93: (185, 267), 94: (221, 259),                        # rig (inner bottom)
    125: (117, 131), 126: (147, 108), 127: (184, 98), 128: (221, 107), 129: (250, 131),  # sub
}
# EVE inventory flag -> loaded-charge icon top-left (px).
_CHG_XY: dict[int, tuple[int, int]] = {
    27: (94, 88), 28: (119, 70), 29: (146, 58), 30: (175, 52),
    31: (204, 52), 32: (232, 60), 33: (258, 72), 34: (280, 91),            # high
    19: (59, 154), 20: (54, 182), 21: (56, 210), 22: (62, 238),
    23: (76, 265), 24: (94, 288), 25: (118, 305), 26: (146, 318),          # mid
    11: (315, 150), 12: (319, 179), 13: (318, 206), 14: (310, 234),
    15: (297, 261), 16: (275, 283), 17: (251, 300), 18: (225, 310),        # low
}
# Flags of each rack in slot order; the SdeType field with the hull's slot count; and the
# build_fit section key (subsystems bucket under "subsystem").
_RACK_FLAGS: dict[str, list[int]] = {
    "high": [27, 28, 29, 30, 31, 32, 33, 34],
    "med": [19, 20, 21, 22, 23, 24, 25, 26],
    "low": [11, 12, 13, 14, 15, 16, 17, 18],
    "rig": [92, 93, 94],
    "sub": [125, 126, 127, 128, 129],
}
_RACK_COUNT_FIELD = {"high": "hi_slots", "med": "med_slots", "low": "low_slots", "rig": "rig_slots"}
_RACK_SECTION = {"high": "high", "med": "med", "low": "low", "rig": "rig", "sub": "subsystem"}


def _pct(left: int, top: int, size: int) -> tuple[float, float]:
    """Icon centre as (x%, y%) of the 398 box, for responsive absolute positioning."""
    return (round((left + size / 2) / _WHEEL_BOX * 100, 3),
            round((top + size / 2) / _WHEEL_BOX * 100, 3))


def build_fit_wheel(killmail, deviation=None) -> dict:
    """The fit as zKillboard's radial fitting window (KB-21b).

    Each module renders at the fixed position for its EVE inventory flag; available-but-empty
    slots (up to the hull's slot count) render as sockets; a loaded charge renders as a
    smaller icon nested inward from its module. Drones are NOT on the wheel (they go to
    ``extras`` with cargo/implants), matching zKill. Builds on :func:`build_fit` for
    names/values/off-doctrine markers.
    """
    fit = build_fit(killmail, deviation)
    sections = {s["key"]: s for s in fit["sections"]}

    by_flag: dict[int, list[dict]] = {}   # a module + its loaded charge share a flag
    for section_key in _RACK_SECTION.values():
        sec = sections.get(section_key)
        if sec:
            for it in sec["items"]:
                if not it["empty"]:
                    by_flag.setdefault(it["flag"], []).append(it)

    hull = (
        SdeType.objects.filter(type_id=killmail.victim_ship_type_id)
        .values(*_CAPACITY_FIELDS).first() or {}
    )

    def _module(flag):
        return next((i for i in by_flag.get(flag, []) if i.get("category_id") != _CHARGE_CATEGORY), None)

    def _charge(flag):
        return next((i for i in by_flag.get(flag, []) if i.get("category_id") == _CHARGE_CATEGORY), None)

    slots, charges = [], []
    for rack, flags in _RACK_FLAGS.items():
        cnt_field = _RACK_COUNT_FIELD.get(rack)
        if rack == "sub":
            count = sum(1 for f in flags if _module(f))   # subs: only as many as are fitted
        else:
            count = hull.get(cnt_field) if cnt_field else None
        available = set(flags[:count]) if count else set()
        fitted = {f for f in flags if _module(f)}
        for f in sorted(available | fitted, key=flags.index):
            x, y = _pct(*_MOD_XY[f], _MOD_PX)
            module = _module(f)
            slots.append({**module, "x": x, "y": y, "empty": False, "rack": rack}
                         if module else {"x": x, "y": y, "empty": True, "rack": rack})
            charge = _charge(f)
            if charge and f in _CHG_XY:
                cx, cy = _pct(*_CHG_XY[f], _CHG_PX)
                charges.append({**charge, "x": cx, "y": cy})

    extras = [s for s in fit["sections"] if s["key"] in ("drone", "implant", "cargo", "other")]
    return {
        "hull_type_id": killmail.victim_ship_type_id,
        "slots": slots,
        "charges": charges,
        "extras": extras,
        "mod_pct": round(_MOD_PX / _WHEEL_BOX * 100, 3),
        "chg_pct": round(_CHG_PX / _WHEEL_BOX * 100, 3),
        "ship_pct": round(256 / _WHEEL_BOX * 100, 3),
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
