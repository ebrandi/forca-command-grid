"""Jump planner orchestration — turn an origin, destination, ship and skills into
one coherent plan: jump-drive legs, stargate legs, fuel and warnings.

This is the single entry point the view calls. It:

1. resolves the route mode from the ship's capabilities
   (:mod:`apps.navigation.route_mode`);
2. for a high-sec endpoint a jump freighter can't cyno into, picks a low-sec
   exit/entry (:mod:`apps.navigation.highsec_exit`) and stitches the jump legs to
   a **real stargate route** from the ESI route engine
   (:mod:`apps.logistics.routing`);
3. computes fuel for the **jump legs only**, in the hull's isotope, with a
   configurable safety margin and an ISK estimate from market prices.

The true destination is always preserved in the result — a low-sec exit is shown
as an intermediate staging point, never as "the destination".
"""
from __future__ import annotations

import math
from decimal import Decimal

from apps.logistics.jumps import effective_range, is_cyno_capable, jump_plan_multi
from apps.logistics.routing import RouteUnavailable, route_plan_multi, security_band
from apps.logistics.ships import ShipProfile
from apps.sde.models import SdeSolarSystem

from .highsec_exit import pair_entry_exit, rank_exits
from .route_mode import RouteMode, resolve_route_mode

# The three jump skills the planner consumes, by their SDE type_id (verified against
# the SDE, not hardcoded from memory — JFC is 21610, not the drive-operation 3456).
JUMP_SKILL_TYPES = {
    "jdc": 21611,  # Jump Drive Calibration — affects range
    "jfc": 21610,  # Jump Fuel Conservation — −10% fuel/level
    "jf": 29029,   # Jump Freighters — −10% fuel/level (freighters only)
}


def jump_skills_for_user(user) -> dict | None:
    """The pilot's trained jump-skill levels from their already-synced skills (4.1).

    Reuses the existing skill snapshot (least-privilege — no new ESI scope; member
    skills are synced by the sync-member-skills beat) to auto-fill the planner from a
    pilot's REAL skills instead of the leadership defaults. Returns
    ``{"jdc","jfc","jf","character"}`` for the pilot's main character, or None when the
    pilot is anonymous, has no linked character, or has no skill snapshot yet.
    """
    from apps.characters.models import CharacterSkillSnapshot

    if not user or not getattr(user, "is_authenticated", False):
        return None
    # user.main_character reads only this user's own characters (is_main → first),
    # so the lookup is inherently ownership-scoped — no cross-user leak (review LOW-1).
    char = getattr(user, "main_character", None)
    if char is None:
        return None
    snap = (
        CharacterSkillSnapshot.objects.filter(character=char, is_latest=True)
        .order_by("-fetched_at")
        .first()
    )
    if snap is None:
        return None
    try:
        return {
            "jdc": snap.trained_level(JUMP_SKILL_TYPES["jdc"]),
            "jfc": snap.trained_level(JUMP_SKILL_TYPES["jfc"]),
            "jf": snap.trained_level(JUMP_SKILL_TYPES["jf"]),
            "character": char,
        }
    except (AttributeError, TypeError, ValueError):
        # A malformed snapshot must never 500 the planner (public, anonymous-safe)
        # — degrade to the leadership defaults, matching _default_price's posture.
        return None


def _sys_row(system) -> dict:
    return {
        "system_id": system.system_id, "name": system.name,
        "security": round(system.security, 1), "band": security_band(system.security),
    }


def _default_price(type_id: int) -> Decimal | None:
    """Jita-sell price for a fuel isotope, or ``None`` if market data is missing."""
    try:
        from apps.market.pricing import price_for

        val = price_for(type_id)
        return val if val and val > 0 else None
    except Exception:  # noqa: BLE001 - pricing is best-effort; never break the planner
        return None


def _enrich_path(path: list[int]) -> list[dict]:
    rows = {
        sid: (name, sec)
        for sid, name, sec in SdeSolarSystem.objects.filter(system_id__in=path)
        .values_list("system_id", "name", "security")
    }
    out = []
    for sid in path:
        name, sec = rows.get(sid, (f"System {sid}", -1.0))
        out.append({"system_id": sid, "name": name, "security": round(sec, 1),
                    "band": security_band(sec)})
    return out


def _jump_segment(plan: dict, profile: ShipProfile, title: str) -> dict:
    """A jump-drive segment from a raw ``jump_plan_multi`` result."""
    waypoints = _enrich_path(plan["path"])
    return {
        "kind": "jump",
        "title": title,
        "cyno_label": profile.cyno_label,
        "jumps": plan["jumps"],
        "ly": plan["total_ly"],
        "fuel": plan["total_fuel"],
        "waypoints": waypoints,
        "hops": plan["hops"],
        "from": waypoints[0] if waypoints else None,
        "to": waypoints[-1] if waypoints else None,
        "travel_min": plan.get("travel_min", 0.0),
        "final_fatigue_min": plan.get("final_fatigue_min", 0.0),
        "warnings": [],
    }


def _gate_segment(route: dict, title: str) -> dict:
    """A stargate segment from a ``route_plan_multi`` result."""
    systems = route["systems"]
    lowsec = [s for s in systems if s["band"] != "highsec"]
    warnings = []
    if lowsec:
        warnings.append(
            f"{len(lowsec)} low/null system(s) on the gate leg: "
            + ", ".join(s["name"] for s in lowsec[:6])
            + ("…" if len(lowsec) > 6 else "")
        )
    return {
        "kind": "gate",
        "title": title,
        "jumps": route["jumps"],
        "systems": systems,
        "from": systems[0] if systems else None,
        "to": systems[-1] if systems else None,
        "warnings": warnings,
    }


def _fail(mode_res, origin, dest, extra=""):
    return {
        "can_plan": False,
        "mode": mode_res.mode,
        "mode_label": mode_res.label,
        "reason": mode_res.reason,
        "error": (mode_res.reason + (" " + extra if extra else "")).strip(),
        "origin": _sys_row(origin), "dest": _sys_row(dest),
        "segments": [], "warnings": [], "exit_candidates": None,
    }


def plan_jump(origin, dest, profile: ShipProfile, *, jdc: int = 5, jfc: int = 5,
              jf_skill: int = 5, jde_rigs: int = 0, custom_range: float | None = None,
              waypoints=None, preference: str = "safer", avoid: set[int] | None = None,
              require_stations: bool = False, prefer_stations: bool = True,
              connections=None, safety_margin_pct: float = 0.0,
              exit_system_id: int | None = None, price_fn=None) -> dict:
    """Plan a trip. Returns a structured plan (see module docstring)."""
    avoid = set(avoid or ())
    waypoints = list(waypoints or [])
    price_fn = price_fn or _default_price
    range_ly = float(custom_range) if custom_range else effective_range(profile.base_range_ly, jdc)

    res = resolve_route_mode(origin.security, dest.security, profile)
    if not res.can_plan:
        hs = [s.name for s in (origin, dest) if security_band(s.security) == "highsec"]
        extra = f"({' and '.join(hs)} is high-sec.)" if hs else ""
        return _fail(res, origin, dest, extra)

    # Waypoints force the *jump* path; they must be cyno-capable (low/null).
    bad_wp = [w.name for w in waypoints if not is_cyno_capable(w.security)]
    if bad_wp:
        return {**_fail(res, origin, dest),
                "error": f"Waypoint {', '.join(bad_wp)} is high-sec — a cyno can't be lit "
                         "there. Waypoints must be low-sec or null-sec."}

    segments: list[dict] = []
    warnings: list[str] = []
    exit_candidates = None
    chosen_exit = chosen_entry = None

    def _jump_only():
        return jump_plan_multi(
            [origin.system_id, *[w.system_id for w in waypoints], dest.system_id],
            range_ly=range_ly, fuel_per_ly=profile.base_fuel_per_ly,
            fatigue_factor=profile.fatigue_factor, uses_jf_skill=profile.jf_skill,
            jfc=jfc, jf_skill=jf_skill, jde_rigs=jde_rigs, avoid=avoid,
            require_stations=require_stations,
        )

    if res.mode in (RouteMode.JUMP_ONLY, RouteMode.BLACK_OPS_JUMP):
        plan = _jump_only()
        if plan is None:
            return {**_fail(res, origin, dest),
                    "error": "No jump route within range — try a longer-range hull/JDC, "
                             "fewer avoided systems/waypoints, or drop ‘dockable only’."}
        segments.append(_jump_segment(plan, profile, f"{origin.name} → {dest.name}"))

    elif res.mode == RouteMode.GATE_ONLY:
        try:
            route = route_plan_multi(
                [origin.system_id, *[w.system_id for w in waypoints], dest.system_id],
                preference, avoid=avoid, connections=connections)
        except RouteUnavailable as exc:
            return {**_fail(res, origin, dest), "error": str(exc)}
        segments.append(_gate_segment(route, f"{origin.name} → {dest.name}"))

    elif res.mode == RouteMode.JUMP_TO_LOWSEC_EXIT_THEN_GATE:
        exit_candidates = rank_exits(dest.system_id, origin.system_id, range_ly,
                                     avoid=avoid, require_stations=require_stations,
                                     prefer_stations=prefer_stations)
        if not exit_candidates:
            return {**_fail(res, origin, dest),
                    "error": f"No low-sec exit near {dest.name} is reachable by jump from "
                             f"{origin.name}. Try a longer-range hull/JDC, a different origin, "
                             "or add a waypoint."}
        def _leg_to_exit(exit_id):
            return jump_plan_multi(
                [origin.system_id, *[w.system_id for w in waypoints], exit_id],
                range_ly=range_ly, fuel_per_ly=profile.base_fuel_per_ly,
                fatigue_factor=profile.fatigue_factor, uses_jf_skill=profile.jf_skill,
                jfc=jfc, jf_skill=jf_skill, jde_rigs=jde_rigs, avoid=avoid,
                require_stations=require_stations)

        chosen_exit, jplan = _first_routable(exit_candidates, exit_system_id, _leg_to_exit)
        if chosen_exit is None:
            return {**_fail(res, origin, dest),
                    "error": f"No low-sec exit near {dest.name} is reachable by jump from "
                             f"{origin.name} with these filters. Try a longer-range hull/JDC, "
                             "fewer avoided systems/waypoints, or dropping ‘dockable only’."}
        ex = SdeSolarSystem.objects.get(system_id=chosen_exit["system_id"])
        gate = _gate_or_fail(ex.system_id, dest.system_id, preference, avoid, connections)
        if isinstance(gate, str):
            return {**_fail(res, origin, dest), "error": gate}
        segments.append(_jump_segment(jplan, profile, f"Jump: {origin.name} → {ex.name} (low-sec exit)"))
        segments.append(_gate_segment(gate, f"Gate: {ex.name} → {dest.name} (high-sec destination)"))
        warnings.append(
            f"{ex.name} ({chosen_exit['security']}) is your low-sec exit — light a cyno "
            f"or use a beacon there, then take {gate['jumps']} gate(s) to {dest.name}.")
        warnings += chosen_exit.get("warnings", [])

    elif res.mode == RouteMode.GATE_TO_JUMP_ENTRY_THEN_JUMP:
        exit_candidates = rank_exits(origin.system_id, dest.system_id, range_ly,
                                     avoid=avoid, require_stations=require_stations,
                                     prefer_stations=prefer_stations)
        if not exit_candidates:
            return {**_fail(res, origin, dest),
                    "error": f"No low-sec staging system near {origin.name} can jump to "
                             f"{dest.name}. Try a longer-range hull/JDC or a different destination."}
        def _leg_from_entry(entry_id):
            return jump_plan_multi(
                [entry_id, *[w.system_id for w in waypoints], dest.system_id],
                range_ly=range_ly, fuel_per_ly=profile.base_fuel_per_ly,
                fatigue_factor=profile.fatigue_factor, uses_jf_skill=profile.jf_skill,
                jfc=jfc, jf_skill=jf_skill, jde_rigs=jde_rigs, avoid=avoid,
                require_stations=require_stations)

        chosen_entry, jplan = _first_routable(exit_candidates, exit_system_id, _leg_from_entry)
        if chosen_entry is None:
            return {**_fail(res, origin, dest),
                    "error": f"No low-sec staging system near {origin.name} can jump to "
                             f"{dest.name} with these filters. Try a longer-range hull/JDC, "
                             "fewer avoided systems/waypoints, or dropping ‘dockable only’."}
        en = SdeSolarSystem.objects.get(system_id=chosen_entry["system_id"])
        gate = _gate_or_fail(origin.system_id, en.system_id, preference, avoid, connections)
        if isinstance(gate, str):
            return {**_fail(res, origin, dest), "error": gate}
        segments.append(_gate_segment(gate, f"Gate: {origin.name} (high-sec) → {en.name}"))
        segments.append(_jump_segment(jplan, profile, f"Jump: {en.name} → {dest.name}"))
        warnings.append(
            f"Gate out of {origin.name} to {en.name} ({chosen_entry['security']}), light a "
            "cyno there, then jump.")
        warnings += chosen_entry.get("warnings", [])

    elif res.mode == RouteMode.MIXED_GATE_AND_JUMP:
        pair = pair_entry_exit(origin.system_id, dest.system_id, range_ly, avoid=avoid,
                               require_stations=require_stations, prefer_stations=prefer_stations)
        if not pair:
            return {**_fail(res, origin, dest),
                    "error": f"Couldn't find a low-sec entry near {origin.name} that can jump to "
                             f"a low-sec exit near {dest.name}. The high-sec pockets may be too "
                             "far apart for this hull's range."}
        en = SdeSolarSystem.objects.get(system_id=pair["entry"]["system_id"])
        ex = SdeSolarSystem.objects.get(system_id=pair["exit"]["system_id"])
        gate_in = _gate_or_fail(origin.system_id, en.system_id, preference, avoid, connections)
        gate_out = _gate_or_fail(ex.system_id, dest.system_id, preference, avoid, connections)
        if isinstance(gate_in, str):
            return {**_fail(res, origin, dest), "error": gate_in}
        if isinstance(gate_out, str):
            return {**_fail(res, origin, dest), "error": gate_out}
        jplan = jump_plan_multi(
            [en.system_id, *[w.system_id for w in waypoints], ex.system_id],
            range_ly=range_ly, fuel_per_ly=profile.base_fuel_per_ly,
            fatigue_factor=profile.fatigue_factor, uses_jf_skill=profile.jf_skill,
            jfc=jfc, jf_skill=jf_skill, jde_rigs=jde_rigs, avoid=avoid,
            require_stations=require_stations)
        if jplan is None:
            return {**_fail(res, origin, dest),
                    "error": "No jump route between the chosen entry and exit systems."}
        chosen_entry, chosen_exit = pair["entry"], pair["exit"]
        segments.append(_gate_segment(gate_in, f"Gate: {origin.name} (high-sec) → {en.name}"))
        segments.append(_jump_segment(jplan, profile, f"Jump: {en.name} → {ex.name}"))
        segments.append(_gate_segment(gate_out, f"Gate: {ex.name} → {dest.name} (high-sec destination)"))
        warnings.append(f"Two gate legs: out of {origin.name} to {en.name}, and from {ex.name} "
                        f"into {dest.name}. Fuel covers the jump between them only.")

    # --- Fuel + ISK across every jump segment ---------------------------------
    jump_fuel = sum(s["fuel"] for s in segments if s["kind"] == "jump")
    cyno_jumps = sum(s["jumps"] for s in segments if s["kind"] == "jump")
    gate_jumps = sum(s["jumps"] for s in segments if s["kind"] == "gate")
    total_ly = round(sum(s["ly"] for s in segments if s["kind"] == "jump"), 2)
    travel_min = round(sum(s.get("travel_min", 0.0) for s in segments if s["kind"] == "jump"), 1)
    fatigue = max((s.get("final_fatigue_min", 0.0) for s in segments if s["kind"] == "jump"),
                  default=0.0)

    margin = max(0.0, float(safety_margin_pct)) / 100.0
    fuel_with_margin = math.ceil(jump_fuel * (1.0 + margin)) if jump_fuel else 0
    unit_price = price_fn(profile.isotope_type_id) if jump_fuel else None
    fuel_isk = (unit_price * fuel_with_margin) if unit_price else None

    assumptions = [
        f"Range {round(range_ly, 2)} ly ({profile.label}, base {profile.base_range_ly} ly × "
        f"JDC {jdc}). JDC affects range only.",
        f"Fuel {int(profile.base_fuel_per_ly)} {profile.isotope_name}/ly base, −10%/level JFC "
        f"(JFC {jfc})" + (f", −10%/level Jump Freighters skill (JF {jf_skill})"
                          if profile.jf_skill else "")
        + (f", {jde_rigs}× Jump Drive Economizer rig" if jde_rigs else "") + ".",
        "Fuel is rounded up per jump (never under-fuel) and covers jump legs only — "
        "gate legs burn no isotopes.",
    ]
    if margin:
        assumptions.append(f"Includes a {safety_margin_pct:g}% fuel safety margin.")
    if unit_price is None and jump_fuel:
        warnings.append("No market price for the fuel isotope — ISK cost unavailable.")

    map_ids = _combined_ids(segments)
    return {
        "can_plan": True,
        "mode": res.mode,
        "mode_label": res.label,
        "reason": res.reason,
        "error": None,
        "is_mixed": res.is_mixed,
        "origin": _sys_row(origin),
        "dest": _sys_row(dest),  # always the TRUE destination
        "segments": segments,
        "exit": chosen_exit,
        "entry": chosen_entry,
        "exit_candidates": exit_candidates,
        "warnings": warnings,
        "assumptions": assumptions,
        "map_ids": map_ids,
        "summary": {
            "cyno_jumps": cyno_jumps,
            "gate_jumps": gate_jumps,
            "total_ly": total_ly,
            "fuel_units": jump_fuel,
            "fuel_with_margin": fuel_with_margin,
            "safety_margin_pct": safety_margin_pct,
            "isotope_type_id": profile.isotope_type_id,
            "isotope_name": profile.isotope_name,
            "fuel_isk": fuel_isk,
            "unit_price": unit_price,
            "travel_min": travel_min,
            "final_fatigue_min": round(fatigue, 1),
        },
        "ship": {
            "key": profile.key, "label": profile.label, "class_label": profile.class_label,
            "isotope_name": profile.isotope_name, "cyno_label": profile.cyno_label,
        },
    }


def _first_routable(candidates: list[dict], preferred_id: int | None, build):
    """Try candidates (the pilot's chosen exit first, then rank order) until one
    yields a real jump plan. This closes the gap where a top-ranked exit passes the
    reachability probe but fails the actual multi-leg build (station-only hops or
    forced waypoints), so the planner falls back instead of reporting no route."""
    ordered = candidates
    if preferred_id:
        ordered = sorted(candidates, key=lambda c: c["system_id"] != preferred_id)
    for cand in ordered:
        plan = build(cand["system_id"])
        if plan is not None:
            return cand, plan
    return None, None


def _gate_or_fail(origin_id, dest_id, preference, avoid, connections):
    try:
        return route_plan_multi([origin_id, dest_id], preference, avoid=avoid,
                                connections=connections)
    except RouteUnavailable as exc:
        return str(exc)


def _combined_ids(segments: list[dict]) -> list[int]:
    """Ordered, de-duplicated system ids across every segment (for the route map)."""
    ids: list[int] = []
    for seg in segments:
        seq = ([w["system_id"] for w in seg["waypoints"]] if seg["kind"] == "jump"
               else [s["system_id"] for s in seg["systems"]])
        for sid in seq:
            if not ids or ids[-1] != sid:
                ids.append(sid)
    return ids
