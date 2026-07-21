"""Presentation-layer localisation of engine diagnostics.

The calculation engine (``apps/fitting/engine``) is deliberately Django-free, so it emits
each diagnostic as a stable ``code`` + structured ``params`` + an English fallback title.
This module — the one place that does it — turns those into translated, human-readable
strings under the active request locale, and turns the engine's ``unsupported`` machine
codes into readable labels. Unknown codes fall back to the engine's English title (or a
de-slugged code), so a newly added engine diagnostic is never rendered blank or as a raw
snake_case token.
"""
from __future__ import annotations

from django.utils.translation import gettext as _

_RESOURCE_TITLES = {
    "cpu_exceeded": lambda: _("CPU exceeded"),
    "powergrid_exceeded": lambda: _("Powergrid exceeded"),
    "calibration_exceeded": lambda: _("Calibration exceeded"),
}
_SLOT_TITLES = {
    "high": lambda: _("Too many high-slot modules"),
    "med": lambda: _("Too many mid-slot modules"),
    "low": lambda: _("Too many low-slot modules"),
    "rig": lambda: _("Too many rig-slot modules"),
}
_UNSUPPORTED_LABELS = {
    "no_weapons_detected": lambda: _("No weapons or drones detected"),
    "turret_application_not_modelled":
        lambda: _("Turret tracking is not modelled (turrets shown at full damage)"),
}


def localise_diagnostic(d: dict) -> dict:
    """A copy of one engine diagnostic dict with title/detail/suggested_action rendered in
    the active language. ``code`` and ``params`` are preserved unchanged."""
    code = d.get("code", "")
    p = d.get("params") or {}
    title = d.get("title", "")
    detail = d.get("detail", "")
    action = d.get("suggested_action", "")

    if code in _RESOURCE_TITLES:
        title = _RESOURCE_TITLES[code]()
        detail = _("%(used)s of %(cap)s used") % {"used": p.get("used"), "cap": p.get("cap")}
        action = _("Remove or downsize a module, or fit a fitting upgrade.")
    elif code == "too_many_modules":
        maker = _SLOT_TITLES.get(p.get("slot"))
        title = maker() if maker else title
        detail = _("%(used)s fitted, %(total)s slots") % {
            "used": p.get("used"), "total": p.get("total")}
    elif code == "turret_hardpoints":
        title = _("Not enough turret hardpoints")
        detail = _("%(have)s turrets, %(cap)s hardpoints") % {
            "have": p.get("have"), "cap": p.get("cap")}
    elif code == "launcher_hardpoints":
        title = _("Not enough launcher hardpoints")
        detail = _("%(have)s launchers, %(cap)s hardpoints") % {
            "have": p.get("have"), "cap": p.get("cap")}
    elif code == "missing_ammo":
        title = _("Weapon has no charge loaded")
        detail = ""   # the raw type id is not user-facing
        action = _("Load a compatible charge.")
    elif code == "rig_size_mismatch":
        title = _("Rig size does not fit this hull")
        detail = ""
        action = _("Fit a rig of this hull's size class.")
    elif code == "max_group_fitted":
        title = _("Too many modules of this group fitted")
        detail = _("At most %(max)s allowed") % {"max": p.get("max")}
    elif code == "max_group_active_exceeded":
        title = _("Too many modules of this group active")
        detail = _("At most %(max)s may be active at once") % {"max": p.get("max")}
        action = _("Set one of them offline or online.")
    elif code == "max_group_online_exceeded":
        title = _("Too many modules of this group online")
        detail = _("At most %(max)s may be online at once") % {"max": p.get("max")}
        action = _("Set one of them offline.")
    elif code == "ship_restriction_violated":
        title = _("Module cannot be fitted to this hull")
        detail = ""
        action = _("Fit this module to a compatible hull.")
    elif code == "implant_slot_conflict":
        title = _("Two implants occupy the same slot")
        detail = ""
        action = _("Remove one of the conflicting implants.")
    elif code == "booster_slot_conflict":
        title = _("Two boosters occupy the same slot")
        detail = ""
        action = _("Remove one of the conflicting boosters.")
    elif code == "subsystem_slot_conflict":
        title = _("Two subsystems occupy the same slot")
        detail = ""
        action = _("Fit one subsystem per slot.")
    elif code == "subsystem_count_invalid":
        title = _("Incomplete subsystem configuration")
        detail = _("%(fitted)s of %(required)s subsystems fitted") % {
            "fitted": p.get("fitted"), "required": p.get("required")}
        action = _("Fit one subsystem in every slot.")
    elif code == "incompatible_charge":
        title = _("Charge not accepted by this module")
        detail = ""
        action = _("Load a charge from a group this module accepts.")
    elif code == "charge_size_mismatch":
        title = _("Charge size does not match the module")
        detail = ""
        action = _("Load a charge of the matching size.")
    elif code == "drone_bandwidth_exceeded":
        title = _("Drone bandwidth exceeded")
        detail = _("%(used)s of %(cap)s Mbit/s") % {"used": p.get("used"), "cap": p.get("cap")}
        action = _("Recall a drone or field smaller drones.")
    elif code == "drone_bay_exceeded":
        title = _("Drone bay volume exceeded")
        detail = _("%(used)s of %(cap)s m3") % {"used": p.get("used"), "cap": p.get("cap")}
    elif code == "drones_over_bandwidth":
        title = _("Not all drones fit in bandwidth")
        detail = _("%(counted)s of %(requested)s counted in the simulation") % {
            "counted": p.get("counted"), "requested": p.get("requested")}

    return {**d, "title": title, "detail": detail, "suggested_action": action}


def localise_diagnostics(diags: list[dict] | None) -> list[dict]:
    return [localise_diagnostic(d) for d in (diags or [])]


def localise_unsupported(codes: list[str] | None) -> list[str]:
    """Turn ``unsupported`` machine codes into readable, translated labels."""
    out = []
    for c in (codes or []):
        maker = _UNSUPPORTED_LABELS.get(c)
        out.append(maker() if maker else str(c).replace("_", " "))
    return out
