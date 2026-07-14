"""Route-mode resolver — decide the *shape* of a jump trip from ship capability.

Given an origin and destination security and a :class:`ShipProfile`, work out
whether the trip is gate-only, jump-only, a mixed jump→gate (or gate→jump), a
Black Ops jump, or simply impossible for the chosen hull. This is the single
decision point the planner consults; it replaces the scattered
``if not is_cyno_capable(...)`` checks the old view carried, and it never keys on
a hull *name* — only on the capability flags in the profile.

Two high-sec rules drive everything (see :class:`ShipProfile`):

* A cyno can't be lit in high-sec, so a jump can neither *end* nor *start*
  there — a high-sec leg is always a stargate leg.
* Whether the hull may use high-sec stargates at all is a *separate* capability
  (``reaches_highsec``). Jump Freighters and Black Ops can; true capitals and
  supercapitals cannot, so a high-sec endpoint is invalid for them.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from apps.logistics.routing import security_band
from apps.logistics.ships import HIGHSEC, ShipProfile


class RouteMode(str, Enum):
    GATE_ONLY = "gate_only"
    JUMP_ONLY = "jump_only"
    BLACK_OPS_JUMP = "black_ops_jump"
    JUMP_TO_LOWSEC_EXIT_THEN_GATE = "jump_to_exit_then_gate"   # high-sec destination
    GATE_TO_JUMP_ENTRY_THEN_JUMP = "gate_from_highsec_then_jump"  # high-sec origin
    MIXED_GATE_AND_JUMP = "mixed_gate_and_jump"                # high-sec at both ends
    INVALID_FOR_DESTINATION = "invalid_for_destination"
    UNSUPPORTED = "unsupported"


# Short human labels for the route-mode badge.
MODE_LABELS = {
    RouteMode.GATE_ONLY: _("Gate route"),
    RouteMode.JUMP_ONLY: _("Jump route"),
    RouteMode.BLACK_OPS_JUMP: _("Black Ops jump"),
    RouteMode.JUMP_TO_LOWSEC_EXIT_THEN_GATE: _("Jump + gate (high-sec exit)"),
    RouteMode.GATE_TO_JUMP_ENTRY_THEN_JUMP: _("Gate + jump (high-sec start)"),
    RouteMode.MIXED_GATE_AND_JUMP: _("Gate + jump + gate"),
    RouteMode.INVALID_FOR_DESTINATION: _("Invalid for this hull"),
    RouteMode.UNSUPPORTED: _("Unsupported"),
}

# Display names for the security bands. These are PROTECTED EVE terms (see
# core/i18n/data/protected-terms.yml) — they are never translated. The map exists so
# prose never interpolates the raw ``security_band()`` slug (``highsec``) verbatim.
BAND_DISPLAY = {
    "highsec": "High-sec",
    "lowsec": "Low-sec",
    "nullsec": "Null-sec",
}


def band_display(band: str) -> str:
    return BAND_DISPLAY.get(band, band)


@dataclass(frozen=True)
class RouteResolution:
    mode: RouteMode
    origin_band: str
    dest_band: str
    reason: str
    can_plan: bool
    needs_exit_dest: bool = False     # high-sec destination → find a low-sec exit + gate in
    needs_entry_origin: bool = False  # high-sec origin → gate out to a low-sec entry, then jump

    @property
    def label(self) -> str:
        return MODE_LABELS.get(self.mode, self.mode.value)

    @property
    def is_mixed(self) -> bool:
        return self.mode in (
            RouteMode.JUMP_TO_LOWSEC_EXIT_THEN_GATE,
            RouteMode.GATE_TO_JUMP_ENTRY_THEN_JUMP,
            RouteMode.MIXED_GATE_AND_JUMP,
        )


def resolve_route_mode(origin_security: float, dest_security: float,
                       profile: ShipProfile) -> RouteResolution:
    """Resolve the route mode for a trip between two systems of the given security."""
    ob = security_band(origin_security)
    db = security_band(dest_security)
    origin_hs = ob == HIGHSEC
    dest_hs = db == HIGHSEC

    # 1) No jump drive → a plain stargate route (if the hull can gate both ends).
    if not profile.has_jump_drive:
        if profile.can_gate_band(ob) and profile.can_gate_band(db):
            return RouteResolution(
                RouteMode.GATE_ONLY, ob, db,
                gettext("This hull has no jump drive — a normal stargate route."), True)
        return RouteResolution(
            RouteMode.INVALID_FOR_DESTINATION, ob, db,
            gettext("A %(ship)s can't reach a %(band)s system by stargate.")
            % {"ship": profile.label, "band": band_display(db)}, False)

    # 2) Both endpoints in low/null → a pure jump-drive route.
    if not origin_hs and not dest_hs:
        if profile.ship_class == "black_ops":
            return RouteResolution(
                RouteMode.BLACK_OPS_JUMP, ob, db,
                gettext("Black Ops jump via %(cyno)s — covert legs, reduced jump fatigue.")
                % {"cyno": profile.cyno_label.lower()}, True)
        return RouteResolution(
            RouteMode.JUMP_ONLY, ob, db,
            gettext("Direct jump-drive route via %(cyno)s.")
            % {"cyno": profile.cyno_label.lower()}, True)

    # 3) A high-sec endpoint is involved. If the hull can't use high-sec gates
    #    at all (true capitals / supers), the route is impossible — don't invent
    #    a misleading low-sec-exit plan.
    #
    #    Three complete sentences rather than one sentence with a joined "origin and
    #    destination" fragment: a translatable unit must be a whole sentence.
    if not profile.reaches_highsec:
        if origin_hs and dest_hs:
            tmpl = gettext(
                "A %(ship)s can't enter high-sec (no stargate access and no cyno in high-sec), "
                "so neither the high-sec origin nor the high-sec destination can be reached. "
                "Choose a low-sec or null-sec system."
            )
        elif dest_hs:
            tmpl = gettext(
                "A %(ship)s can't enter high-sec (no stargate access and no cyno in high-sec), "
                "so the high-sec destination can't be reached. Choose a low-sec or null-sec system."
            )
        else:
            tmpl = gettext(
                "A %(ship)s can't enter high-sec (no stargate access and no cyno in high-sec), "
                "so the high-sec origin can't be reached. Choose a low-sec or null-sec system."
            )
        return RouteResolution(
            RouteMode.INVALID_FOR_DESTINATION, ob, db,
            tmpl % {"ship": profile.label}, False)

    # 4) Jump Freighter / Black Ops with a high-sec endpoint → mixed jump+gate.
    #    A cyno can't be lit in high-sec, so the high-sec leg is flown on gates.
    if origin_hs and dest_hs:
        return RouteResolution(
            RouteMode.MIXED_GATE_AND_JUMP, ob, db,
            gettext(
                "A %(ship)s can't cyno into or out of high-sec: gate out of the high-sec origin "
                "to a low-sec entry, jump, then gate to the high-sec destination."
            ) % {"ship": profile.label},
            True, needs_exit_dest=True, needs_entry_origin=True)
    if dest_hs:
        return RouteResolution(
            RouteMode.JUMP_TO_LOWSEC_EXIT_THEN_GATE, ob, db,
            gettext(
                "A %(ship)s can't cyno directly into high-sec: jump to a low-sec exit near the "
                "destination, then take stargates the rest of the way."
            ) % {"ship": profile.label},
            True, needs_exit_dest=True)
    return RouteResolution(
        RouteMode.GATE_TO_JUMP_ENTRY_THEN_JUMP, ob, db,
        gettext(
            "A %(ship)s can't light a cyno in high-sec: gate out of the high-sec origin to a "
            "low-sec staging system, then jump from there."
        ) % {"ship": profile.label},
        True, needs_entry_origin=True)
