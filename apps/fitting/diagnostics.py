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
