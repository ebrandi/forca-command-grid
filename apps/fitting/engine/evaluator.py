"""Engine v2 pass 4 — telemetry derived from the graph-evaluated attribute values.

`evaluate()` here replaces `dogma.evaluate()` as the calculation path (see the
calculation-engine ADR). Passes 1-3 (graph.py) produce per-entity evaluated attributes;
this module derives the displayed statistics from them and performs fit validation.

Corrections vs engine v1 locked in here (each carries a regression test):
* Capacitor/shield recharge peak is 2.5·C/τ at 25% charge (the EVE recharge ODE
  dC/dt = (10·C/τ)·(√x − x), x = C/Cmax) — v1 used 0.5·C/τ, five times too small.
* Active tank (boost/repair per second), passive shield regen, turret optimal/falloff/
  tracking, missile velocity/flight/range are computed and reported.
* Drone bandwidth/bay are validated; drones beyond bandwidth do not add DPS.
* Charge compatibility (accepted groups + charge size) and rig size are validated.
* maxGroupFitted is enforced.
* Cap-booster injection counts toward capacitor stability.
* Offline/online/active/overheated gating comes from the graph's effect categories.

Where a mechanic is not modelled the result says so in ``unsupported`` — never a
silent zero.
"""
from __future__ import annotations

import math
from time import perf_counter

from . import attributes as A
from .graph import (
    STATE_ACTIVE,
    Entity,
    EvaluatedFit,
    evaluate_attributes,
)
from .types import (
    AttributeTrace,
    Contribution,
    Diagnostic,
    FitInput,
    FittingResult,
    MissingSkill,
    ModuleState,
    OperatingProfile,
    Severity,
    SkillProfile,
    SlotKind,
    Status,
)

# Dogma attribute ids used by pass 4 beyond attributes.py's named set.
ATTR_CAPACITY = 38
ATTR_VOLUME = 161
ATTR_MAX_GROUP_FITTED = 1544
ATTR_MAX_GROUP_ACTIVE = 763
ATTR_RIG_SIZE = 1547
ATTR_DRONE_BANDWIDTH_USED = 1272
ATTR_CAP_BOOSTER_BONUS = 67
ATTR_RELOAD_TIME = 1795
# Reload-aware sustained DPS (magazine + frequency-crystal wear). Ids verified against
# the SDE (scout-data §B): capacity 38 / volume 161 give the magazine size, chargeRate
# 56 the charges consumed per cycle; crystalsGetDamaged 786 flags a depleting lens whose
# expected life is governed by crystalVolatilityChance 783 / crystalVolatilityDamage 784
# and the crystal's hp (9).
ATTR_CHARGE_RATE = 56
ATTR_CRYSTALS_GET_DAMAGED = 786
ATTR_CRYSTAL_VOL_CHANCE = 783
ATTR_CRYSTAL_VOL_DAMAGE = 784
ATTR_CHARGE_HP = 9
ATTR_MISSILE_VELOCITY = 37
ATTR_EXPLOSION_DELAY = 281
ATTR_SHIELD_RECHARGE = 479
ATTR_SHIELD_BOOST = 68
ATTR_ARMOR_REPAIR = 84
ATTR_HULL_REPAIR = 87           # structureDamageAmount (hull repairers)
ATTR_DURATION = 73
_RECHARGE_PEAK = 2.5            # peak dC/dt = 2.5·Cmax/τ, at 25% charge
AU_METERS = 149_597_870_700    # one astronomical unit in metres (CCP's warp-maths unit)
_DRONE_MOBILE_MIN_SPEED = 1.0  # maxVelocity above this = a chasing (mobile) drone, not a
#                                sentry (pyfa's `droneSpeed > 1` sentry/mobile split)

_RACKED = (SlotKind.HIGH, SlotKind.MED, SlotKind.LOW, SlotKind.RIG, SlotKind.SUBSYSTEM)

_SUBSYSTEM_ADD = {
    A.HI_SLOTS: A.SUB_HI_SLOT_MOD, A.MED_SLOTS: A.SUB_MED_SLOT_MOD,
    A.LOW_SLOTS: A.SUB_LOW_SLOT_MOD,
    A.TURRET_HARDPOINTS: A.SUB_TURRET_HP_MOD,
    A.LAUNCHER_HARDPOINTS: A.SUB_LAUNCHER_HP_MOD,
}


def _active(e: Entity) -> bool:
    return e.module_state in (ModuleState.ACTIVE, ModuleState.OVERHEATED)


def _cycle_ms(ev: EvaluatedFit, e: Entity) -> float:
    v = ev.value(e, ATTR_DURATION)
    if v <= 0:
        v = ev.value(e, A.RATE_OF_FIRE)
    return v


def evaluate(fit: FitInput, skills: SkillProfile, op_profile: OperatingProfile,
             provider) -> FittingResult:
    t0 = perf_counter()
    result = FittingResult(status=Status.VALID,
                           data_version=getattr(provider, "data_version", ""))
    ship_info = provider.type_info(fit.ship_type_id)
    if not ship_info or not provider.attrs(fit.ship_type_id):
        result.status = Status.IMPOSSIBLE
        result.errors.append("unknown_ship")
        result.compute_ms = (perf_counter() - t0) * 1000
        return result

    skill_getter = getattr(provider, "trained_skill_ids", None)
    skill_ids = skill_getter() if skill_getter else []
    ev = evaluate_attributes(fit, skills, provider, skill_ids=skill_ids)

    # Subsystem slot/hardpoint additions are applied by the client outside dogma (the
    # slotModifier effect carries no modifiers) — fold them here, documented as a patch.
    sub_add = {attr: 0.0 for attr in _SUBSYSTEM_ADD}
    for m in ev.modules:
        if m.slot == SlotKind.SUBSYSTEM and m.module_state != ModuleState.OFFLINE:
            for ship_attr, sub_attr in _SUBSYSTEM_ADD.items():
                sub_add[ship_attr] += m.base.get(sub_attr, 0.0)

    resources = _resources(ev, provider, result, sub_add)
    defence = _defence(ev, op_profile, result)
    capacitor = _capacitor(ev, result)
    offence = _offence(ev, provider, op_profile, result)
    mobility = _mobility(ev, op_profile, result)
    targeting = _targeting(ev, op_profile)
    utility = {"cargo": round(ev.ship_value(ATTR_CAPACITY), 1),
               "drone_bay": round(ev.ship_value(A.DRONE_CAPACITY), 1)}
    ewar = _ewar(ev)
    _validate_restrictions(ev, provider, result)
    _validate_mode(ev, provider, result)

    ship_section = {"type_id": fit.ship_type_id, "name": ship_info.get("name", "")}
    if ev.mode is not None:
        # Echo the selected mode (even when invalid — the diagnostic flags that) so the UI
        # can render which tactical mode drove these numbers.
        mode_info = provider.type_info(ev.mode.type_id) or {}
        ship_section["mode"] = {"type_id": ev.mode.type_id,
                                "name": mode_info.get("name", "")}
    result.telemetry = {
        "resources": resources, "defence": defence, "capacitor": capacitor,
        "offence": offence, "mobility": mobility, "targeting": targeting,
        "utility": utility, "ewar": ewar,
        "ship": ship_section,
        "operating_profile": {
            "propulsion_active": op_profile.propulsion_active,
            "damage_profile": op_profile.damage_profile.normalised().as_map(),
        },
    }
    for d in sorted(set(ev.diagnostics)):
        result.unsupported.append(d)
    result.missing_skills = _missing_skills(fit, skills, provider)
    _finalise_status(result)
    result.compute_ms = (perf_counter() - t0) * 1000
    return result


# --------------------------------------------------------------------------- #
# Resources + structural validation
# --------------------------------------------------------------------------- #
def _resources(ev: EvaluatedFit, provider, result: FittingResult, sub_add) -> dict:
    cpu_out = ev.ship_value(A.CPU_OUTPUT)
    pg_out = ev.ship_value(A.POWER_OUTPUT)
    cal_out = ev.ship_value(A.CALIBRATION)
    result.traces["cpu_output"] = AttributeTrace(
        "cpu_output", ev.ship.base.get(A.CPU_OUTPUT, 0.0), round(cpu_out, 2), "tf")
    result.traces["pg_output"] = AttributeTrace(
        "pg_output", ev.ship.base.get(A.POWER_OUTPUT, 0.0), round(pg_out, 2), "MW")

    cpu_used = pg_used = cal_used = 0.0
    slot_counts = {k: 0 for k in ("high", "med", "low", "rig", "subsystem", "drone")}
    turrets = launchers = 0
    group_counts: dict[int, int] = {}
    ship_rig_size = ev.ship.base.get(ATTR_RIG_SIZE)

    for m in ev.modules:
        if m.slot in (SlotKind.HIGH, SlotKind.MED, SlotKind.LOW) \
                and m.module_state != ModuleState.OFFLINE:
            cpu_used += ev.value(m, A.CPU_USAGE)
            pg_used += ev.value(m, A.POWER_USAGE)
        if m.slot == SlotKind.RIG:
            cal_used += m.base.get(A.CALIBRATION_COST, 0.0)
            rig_size = m.base.get(ATTR_RIG_SIZE)
            if ship_rig_size is not None and rig_size is not None \
                    and rig_size != ship_rig_size:
                result.diagnostics.append(Diagnostic(
                    "rig_size_mismatch", Severity.ERROR, "Rig size does not fit this hull",
                    detail=f"type {m.type_id}", contextual=False,
                    params={"type_id": m.type_id, "rig_size": rig_size,
                            "ship_rig_size": ship_rig_size}))
        key = m.slot.value if m.slot.value in slot_counts else None
        if key:
            slot_counts[key] += 1
        if m.slot == SlotKind.HIGH:
            if (A.TURRET_EFFECTS & m.effect_ids) or (
                    m.module_state == ModuleState.OFFLINE
                    and A.TURRET_EFFECTS & provider.effects(m.type_id)):
                turrets += 1
            elif A.EFFECT_LAUNCHER in m.effect_ids or (
                    m.module_state == ModuleState.OFFLINE
                    and A.EFFECT_LAUNCHER in provider.effects(m.type_id)):
                launchers += 1
        if m.group_id is not None:
            group_counts[m.group_id] = group_counts.get(m.group_id, 0) + 1
        max_fitted = m.base.get(ATTR_MAX_GROUP_FITTED)
        if max_fitted and group_counts.get(m.group_id, 0) > int(max_fitted):
            result.diagnostics.append(Diagnostic(
                "max_group_fitted", Severity.ERROR,
                "Too many modules of this group fitted",
                detail=f"group {m.group_id}", contextual=False,
                params={"group_id": m.group_id, "max": int(max_fitted)}))
        _validate_charge(ev, m, result)

    slot_counts["drone"] = len(ev.drones)
    hull = {
        "high": int(ev.ship_value(A.HI_SLOTS) + sub_add[A.HI_SLOTS]),
        "med": int(ev.ship_value(A.MED_SLOTS) + sub_add[A.MED_SLOTS]),
        "low": int(ev.ship_value(A.LOW_SLOTS) + sub_add[A.LOW_SLOTS]),
        "rig": int(ev.ship_value(A.RIG_SLOTS)),
    }
    turret_hp = int(ev.ship_value(A.TURRET_HARDPOINTS) + sub_add[A.TURRET_HARDPOINTS])
    launcher_hp = int(ev.ship_value(A.LAUNCHER_HARDPOINTS)
                      + sub_add[A.LAUNCHER_HARDPOINTS])

    for label, used, cap, code in (
        ("CPU", cpu_used, cpu_out, "cpu_exceeded"),
        ("Powergrid", pg_used, pg_out, "powergrid_exceeded"),
        ("Calibration", cal_used, cal_out, "calibration_exceeded"),
    ):
        if used > cap + 1e-6:
            result.diagnostics.append(Diagnostic(
                code, Severity.ERROR, f"{label} exceeded",
                detail=f"{used:.1f} of {cap:.1f} used", evidence=f"{used - cap:.1f} over",
                suggested_action="Remove or downsize a module, or fit a fitting upgrade.",
                contextual=False,
                params={"used": round(used, 1), "cap": round(cap, 1),
                        "over": round(used - cap, 1)}))
    for slot in ("high", "med", "low", "rig"):
        if slot_counts[slot] > hull[slot]:
            result.diagnostics.append(Diagnostic(
                "too_many_modules", Severity.ERROR, f"Too many {slot}-slot modules",
                detail=f"{slot_counts[slot]} fitted, {hull[slot]} slots",
                contextual=False,
                params={"slot": slot, "used": slot_counts[slot], "total": hull[slot]}))
    if turrets > turret_hp:
        result.diagnostics.append(Diagnostic(
            "turret_hardpoints", Severity.ERROR, "Not enough turret hardpoints",
            detail=f"{turrets} turrets, {turret_hp} hardpoints", contextual=False,
            params={"have": turrets, "cap": turret_hp}))
    if launchers > launcher_hp:
        result.diagnostics.append(Diagnostic(
            "launcher_hardpoints", Severity.ERROR, "Not enough launcher hardpoints",
            detail=f"{launchers} launchers, {launcher_hp} hardpoints", contextual=False,
            params={"have": launchers, "cap": launcher_hp}))

    # Drone bandwidth / bay.
    bandwidth = ev.ship_value(A.DRONE_BANDWIDTH)
    bay = ev.ship_value(A.DRONE_CAPACITY)
    bw_used = sum(ev.value(d, ATTR_DRONE_BANDWIDTH_USED) * d.quantity for d in ev.drones)
    bay_used = sum(d.base.get(ATTR_VOLUME, 0.0) * d.quantity for d in ev.drones)
    if bw_used > bandwidth + 1e-6:
        result.diagnostics.append(Diagnostic(
            "drone_bandwidth_exceeded", Severity.ERROR, "Drone bandwidth exceeded",
            detail=f"{bw_used:.0f} of {bandwidth:.0f} Mbit/s", contextual=False,
            params={"used": round(bw_used, 1), "cap": round(bandwidth, 1)}))
    if bay_used > bay + 1e-6:
        result.diagnostics.append(Diagnostic(
            "drone_bay_exceeded", Severity.ERROR, "Drone bay volume exceeded",
            detail=f"{bay_used:.0f} of {bay:.0f} m3", contextual=False,
            params={"used": round(bay_used, 1), "cap": round(bay, 1)}))

    return {
        "cpu": {"used": round(cpu_used, 2), "output": round(cpu_out, 2)},
        "powergrid": {"used": round(pg_used, 2), "output": round(pg_out, 2)},
        "calibration": {"used": round(cal_used, 2), "output": round(cal_out, 2)},
        "slots": {"used": slot_counts, "hull": hull},
        "hardpoints": {"turret": {"used": turrets, "total": turret_hp},
                       "launcher": {"used": launchers, "total": launcher_hp}},
        "drone_bandwidth": round(bandwidth, 1),
        "drone_bandwidth_used": round(bw_used, 1),
        "drone_bay": round(bay, 1),
        "drone_bay_used": round(bay_used, 1),
    }


def _validate_charge(ev: EvaluatedFit, m: Entity, result: FittingResult) -> None:
    if m.charge is None:
        return
    groups = {int(m.base[a]) for a in A.CHARGE_GROUP_ATTRS if a in m.base}
    if groups and m.charge.group_id not in groups:
        result.diagnostics.append(Diagnostic(
            "incompatible_charge", Severity.ERROR, "Charge not accepted by this module",
            detail=f"module {m.type_id} charge {m.charge.type_id}", contextual=False,
            params={"type_id": m.type_id, "charge_type_id": m.charge.type_id}))
        return
    wsize = m.base.get(A.CHARGE_SIZE)
    csize = m.charge.base.get(A.CHARGE_SIZE)
    if wsize is not None and csize is not None and wsize != csize:
        result.diagnostics.append(Diagnostic(
            "charge_size_mismatch", Severity.ERROR, "Charge size does not match",
            detail=f"module {m.type_id} charge {m.charge.type_id}", contextual=False,
            params={"type_id": m.type_id, "charge_type_id": m.charge.type_id,
                    "module_size": wsize, "charge_size": csize}))


# --------------------------------------------------------------------------- #
# Fit-legality validation (WS-3): group active/online caps, hull restrictions,
# implant/booster/subsystem slot integrity. Every rule fires only when the entity
# actually carries the governing attribute — absence means "unrestricted", never a
# violation. Semantics studied for BEHAVIOUR ONLY in pyfa's GPL eos (no code reused):
#   * hull restriction  — eos/saveddata/fit.py:490-512 (Fit.canFit): fitsToShipType is
#     folded into the TYPE whitelist alongside canFitShipType*, and a module with BOTH a
#     group and a type list fits if EITHER matches (the whitelists union, ship passes if
#     its group OR its type is listed).
#   * maxGroupActive/Online — eos/saveddata/module.py:766-791 (canHaveState): count the
#     modules of a group in state ≥ ACTIVE / ≥ ONLINE, violation when the count exceeds
#     the cap (strict >). maxGroupFitted is handled separately in _resources.
#   * subsystem slot uniqueness — module.py:694-700: two subsystems may not share a
#     subSystemSlot.
# All of these make a fit structurally IMPOSSIBLE (see _finalise_status), mirroring the
# existing max_group_fitted severity.
# --------------------------------------------------------------------------- #
def _validate_restrictions(ev: EvaluatedFit, provider, result: FittingResult) -> None:
    ship_group = ev.ship.group_id
    ship_type = ev.ship.type_id
    active_by_group: dict[int, int] = {}
    online_by_group: dict[int, int] = {}
    subs: list[Entity] = []

    for m in ev.modules:
        if m.slot == SlotKind.SUBSYSTEM:
            subs.append(m)
        _validate_ship_restriction(m, ship_group, ship_type, result)
        if m.group_id is None:
            continue
        online = m.module_state != ModuleState.OFFLINE
        active = m.module_state in (ModuleState.ACTIVE, ModuleState.OVERHEATED)
        if online:
            online_by_group[m.group_id] = online_by_group.get(m.group_id, 0) + 1
            max_online = m.base.get(A.MAX_GROUP_ONLINE)
            if max_online and online_by_group[m.group_id] > int(max_online):
                result.diagnostics.append(Diagnostic(
                    "max_group_online_exceeded", Severity.ERROR,
                    "Too many modules of this group online",
                    detail=f"group {m.group_id}", contextual=False,
                    params={"group_id": m.group_id, "max": int(max_online)}))
        if active:
            active_by_group[m.group_id] = active_by_group.get(m.group_id, 0) + 1
            max_active = m.base.get(A.MAX_GROUP_ACTIVE)
            if max_active and active_by_group[m.group_id] > int(max_active):
                result.diagnostics.append(Diagnostic(
                    "max_group_active_exceeded", Severity.ERROR,
                    "Too many modules of this group active",
                    detail=f"group {m.group_id}", contextual=False,
                    params={"group_id": m.group_id, "max": int(max_active)}))

    _validate_slot_conflicts(ev, result)
    _validate_subsystems(subs, ev, provider, result)


def _validate_mode(ev: EvaluatedFit, provider, result: FittingResult) -> None:
    """A tactical mode must belong to its hull (see graph.mode_valid_for_ship): the mode
    is a "Ship Modifiers" type whose name begins with the hull's name. A mode set on a
    non-T3D hull, or a T3D's mode set on the wrong hull, is structurally IMPOSSIBLE. A T3D
    with no mode is valid and evaluates bare (no diagnostic)."""
    if ev.mode is None or ev.mode_valid:
        return
    mode_info = provider.type_info(ev.mode.type_id) or {}
    ship_info = provider.type_info(ev.ship.type_id) or {}
    result.diagnostics.append(Diagnostic(
        "mode_invalid_for_ship", Severity.ERROR,
        "Tactical mode does not match this hull",
        detail=f"mode {ev.mode.type_id}", contextual=False,
        params={"mode_type_id": ev.mode.type_id, "mode_name": mode_info.get("name", ""),
                "ship_type_id": ev.ship.type_id, "ship_name": ship_info.get("name", "")}))


def _validate_ship_restriction(m: Entity, ship_group: int | None, ship_type: int,
                               result: FittingResult) -> None:
    fits_group = {int(m.base[a]) for a in A.CAN_FIT_SHIP_GROUP_ATTRS if m.base.get(a)}
    fits_type = {int(m.base[a]) for a in A.CAN_FIT_SHIP_TYPE_ATTRS if m.base.get(a)}
    if m.base.get(A.FITS_TO_SHIP_TYPE):
        fits_type.add(int(m.base[A.FITS_TO_SHIP_TYPE]))
    if not (fits_group or fits_type):
        return                                  # module carries no hull restriction
    if ship_group in fits_group or ship_type in fits_type:
        return                                  # ship group OR type is whitelisted
    result.diagnostics.append(Diagnostic(
        "ship_restriction_violated", Severity.ERROR,
        "Module cannot be fitted to this hull",
        detail=f"type {m.type_id}", contextual=False,
        params={"type_id": m.type_id, "ship_group_id": ship_group,
                "allowed_groups": sorted(fits_group), "allowed_types": sorted(fits_type)}))


def _validate_slot_conflicts(ev: EvaluatedFit, result: FittingResult) -> None:
    """Two implants sharing an implantness slot, or two boosters sharing a boosterness
    slot, cannot coexist (the second occupant conflicts)."""
    for kind, slot, attr, code in (
        ("implant", SlotKind.IMPLANT, A.IMPLANTNESS, "implant_slot_conflict"),
        ("booster", SlotKind.BOOSTER, A.BOOSTERNESS, "booster_slot_conflict"),
    ):
        seen: dict[int, int] = {}
        for imp in ev.implants:
            if imp.slot != slot or imp.base.get(attr) is None:
                continue
            key = int(imp.base[attr])
            if key in seen:
                result.diagnostics.append(Diagnostic(
                    code, Severity.ERROR, f"Two {kind}s occupy the same slot",
                    detail=f"slot {key}", contextual=False,
                    params={"slot": key, "type_id": imp.type_id,
                            "conflicts_with": seen[key]}))
            else:
                seen[key] = imp.type_id


def _validate_subsystems(subs: list[Entity], ev: EvaluatedFit, provider,
                         result: FittingResult) -> None:
    seen: dict[int, int] = {}
    for s in subs:
        slot = s.base.get(A.SUBSYSTEM_SLOT)
        if slot is None:
            continue
        key = int(slot)
        if key in seen:
            result.diagnostics.append(Diagnostic(
                "subsystem_slot_conflict", Severity.ERROR,
                "Two subsystems occupy the same slot",
                detail=f"slot {key}", contextual=False,
                params={"slot": key, "type_id": s.type_id, "conflicts_with": seen[key]}))
        else:
            seen[key] = s.type_id

    # A Strategic Cruiser (hull carries maxSubSystems) is only a valid ship with one
    # subsystem in every slot. The required count comes from the subsystem catalogue, not
    # the stale maxSubSystems value (see adapter.subsystem_slots_for_hull).
    if A.MAX_SUBSYSTEMS not in ev.ship.base:
        return
    slot_count = getattr(provider, "subsystem_slots_for_hull", None)
    required = slot_count(ev.ship.type_id) if slot_count else 0
    if required and len(subs) != required:
        result.diagnostics.append(Diagnostic(
            "subsystem_count_invalid", Severity.ERROR,
            "Incomplete subsystem configuration",
            detail=f"{len(subs)} of {required} subsystems", contextual=False,
            params={"fitted": len(subs), "required": required}))


# --------------------------------------------------------------------------- #
# Defence
# --------------------------------------------------------------------------- #
def _defence(ev: EvaluatedFit, op_profile: OperatingProfile, result) -> dict:
    dp = op_profile.damage_profile.normalised().as_map()
    layers = {}
    total_ehp = 0.0
    for name, hp_attr, res_attrs in (
        ("shield", A.SHIELD_HP, A.SHIELD_RESONANCE),
        ("armor", A.ARMOR_HP, A.ARMOR_RESONANCE),
        ("hull", A.HULL_HP, A.HULL_RESONANCE),
    ):
        hp = ev.ship_value(hp_attr)
        res = {d: max(0.0, min(1.0, ev.ship_value(attr)))
               for d, attr in res_attrs.items()}
        weighted = sum(dp[d] * res[d] for d in A.DAMAGE_TYPES)
        ehp = hp / weighted if weighted > 0 else hp
        total_ehp += ehp
        layers[name] = {
            "hp": round(hp, 1),
            "resists": {d: round((1.0 - res[d]) * 100, 1) for d in A.DAMAGE_TYPES},
            "ehp": round(ehp, 1),
        }
        result.traces[f"{name}_hp"] = AttributeTrace(
            f"{name}_hp", ev.ship.base.get(hp_attr, 0.0), round(hp, 1), "HP")

    # Passive shield regeneration: peak = 2.5 · shieldCapacity / τ  (τ = attr 479 ms).
    shield_hp = layers["shield"]["hp"]
    tau_s = ev.ship_value(ATTR_SHIELD_RECHARGE) / 1000.0
    passive_peak = _RECHARGE_PEAK * shield_hp / tau_s if tau_s > 0 else 0.0

    # Active tank: repair/boost per second from ACTIVE local reps, using evaluated
    # amount and cycle time (ancillary charge multipliers arrive via the graph).
    reps = {"shield": 0.0, "armor": 0.0, "hull": 0.0}
    for m in ev.modules:
        if not _active(m):
            continue
        cycle = _cycle_ms(ev, m)
        if cycle <= 0:
            continue
        for layer, attr in (("shield", ATTR_SHIELD_BOOST),
                            ("armor", ATTR_ARMOR_REPAIR),
                            ("hull", ATTR_HULL_REPAIR)):
            amount = ev.value(m, attr) if attr in m.base else 0.0
            if amount:
                reps[layer] += amount / (cycle / 1000.0)

    dp_weighted = sum(dp[d] * max(0.0, min(1.0, ev.ship_value(A.SHIELD_RESONANCE[d])))
                      for d in A.DAMAGE_TYPES)
    return {
        "layers": layers, "ehp_total": round(total_ehp, 1),
        "damage_profile": {d: round(dp[d] * 100, 1) for d in A.DAMAGE_TYPES},
        "passive_shield_regen": {
            "peak_hps": round(passive_peak, 1),
            "recharge_time_s": round(tau_s, 1),
            "peak_ehps": round(passive_peak / dp_weighted, 1) if dp_weighted > 0 else 0.0,
        },
        "active_tank": {
            "shield_hps": round(reps["shield"], 1),
            "armor_hps": round(reps["armor"], 1),
            "hull_hps": round(reps["hull"], 1),
            "total_hps": round(sum(reps.values()), 1),
        },
    }


# --------------------------------------------------------------------------- #
# Capacitor  (peak = 2.5·C/τ; stability from √x-equilibrium; injection counted)
# --------------------------------------------------------------------------- #
def _capacitor(ev: EvaluatedFit, result) -> dict:
    capacity = ev.ship_value(A.CAP_CAPACITY)
    tau = ev.ship_value(A.CAP_RECHARGE_RATE) / 1000.0
    peak = _RECHARGE_PEAK * capacity / tau if tau > 0 else 0.0

    drain = injection = 0.0
    for m in ev.modules:
        if not _active(m):
            continue
        cycle_ms = _cycle_ms(ev, m)
        if cycle_ms <= 0:
            continue
        need = ev.value(m, A.CAP_NEED) if A.CAP_NEED in m.base else 0.0
        if need:
            drain += need / (cycle_ms / 1000.0)
        # Cap booster: injects the charge's capacitorBonus once per cycle (reload time
        # included in the effective cycle so sustained injection isn't overstated).
        if m.charge is not None and ATTR_CAP_BOOSTER_BONUS in m.charge.base:
            bonus = ev.value(m.charge, ATTR_CAP_BOOSTER_BONUS)
            reload_ms = m.base.get(ATTR_RELOAD_TIME, 10000.0)
            eff_cycle = cycle_ms + reload_ms
            if bonus and eff_cycle > 0:
                injection += bonus / (eff_cycle / 1000.0)

    net_drain = drain - injection
    # Equilibrium: net_drain = (10·C/τ)·(√x − x). Let s=√x: s(1−s) = net·τ/(10·C).
    stable = peak > 0 and net_drain <= peak
    stable_pct = None
    runtime_s = None
    if peak > 0 and capacity > 0 and tau > 0:
        if net_drain <= 0:
            stable, stable_pct = True, 100.0
        else:
            k = net_drain * tau / (10.0 * capacity)
            disc = 1.0 - 4.0 * k
            if disc >= 0:
                s = (1.0 + math.sqrt(disc)) / 2.0
                stable, stable_pct = True, round(s * s * 100.0, 1)
            else:
                stable = False
                runtime_s = _cap_runtime(capacity, tau, net_drain)
    return {
        "capacity": round(capacity, 1),
        "recharge_s": round(tau, 1),
        "peak_recharge": round(peak, 2),
        "usage": round(drain, 2),
        "injection": round(injection, 2),
        "stable": stable,
        "stable_pct": stable_pct,
        "runtime_s": runtime_s,
    }


def _cap_runtime(capacity: float, tau: float, net_drain: float) -> float | None:
    """Time from full capacitor to empty under constant net drain, integrating the
    recharge ODE dC/dt = (10·C/τ)(√x − x) − drain with a fixed 1 s step (bounded)."""
    c = capacity
    for t in range(0, 7200):
        x = c / capacity
        c += (10.0 * capacity / tau) * (math.sqrt(x) - x) - net_drain
        if c <= 0:
            return float(t + 1)
    return None


# --------------------------------------------------------------------------- #
# Turret / drone application (hit-quality maths)
# --------------------------------------------------------------------------- #
# Studied for BEHAVIOUR ONLY in pyfa's GPL eos — no code reused. Citations are file:line
# in the pyfa clone (graphs/data/fitDamageStats/calc/application.py, eos/calc.py):
#
# * Chance-to-hit factors multiply (application.py:381-393):
#     CTH = rangeFactor × trackingFactor
#   rangeFactor (eos/calc.py:53-65, turrets pass restrictedRange=False so it never zeroes):
#     0.5 ** ((max(0, distance − optimal) / falloff) ** 2)
#   trackingFactor (application.py:411-413):
#     0.5 ** (((angular × optimalSigRadius) / (tracking × targetSig)) ** 2)
#   Since both are 0.5**x, the product is 0.5 ** (rangeExp + trackExp) — the community-
#   documented closed form (EVE University "Turret mechanics#Hit Math").
#
# * Expected per-shot damage multiplier from CTH (application.py:365-378). The EVE roll
#   model draws x ~ U[0,1): the shot hits iff x < CTH; a hit with x < 0.01 is a *wrecking*
#   shot dealing 3× base, otherwise a normal hit dealing (0.49 + x)× base. Taking the
#   expectation over x:
#       E[mult] = ∫₀^min(CTH,0.01) 3 dx  +  ∫_0.01^CTH (0.49 + x) dx      (CTH > 0.01)
#               = min(CTH,0.01)·3  +  (CTH − 0.01)·((0.01 + CTH)/2 + 0.49)
#     and for CTH ≤ 0.01 the whole hit region is wrecking: E[mult] = CTH·3.
#   This is exactly pyfa's `_calcTurretMult`. Note E[mult](1.0) = 1.01505 > 1: a perfect
#   shot averages slightly above paper DPS because of the wrecking bonus.
#
# * Baseline convention (DECISION — documented in the mechanics matrix): pyfa multiplies
#   nominal DPS by E[mult] directly, so its DPS-vs-range graph shows that ~1.5% wrecking
#   overshoot at point-blank. We instead NORMALISE by E[mult](1.0) so a perfect-application
#   shot reports applied == raw and application never exceeds 1.0 — matching the convention
#   our missile path already uses (`_missile_application` returns min(1, …)). This keeps a
#   single "application %" meaning across turrets, drones and missiles.
def _expected_turret_mult(cth: float) -> float:
    """Expected turret damage multiplier (incl. wrecking shots) for a chance-to-hit."""
    wrecking = min(cth, 0.01) * 3.0
    normal_chance = cth - min(cth, 0.01)
    if normal_chance > 0:
        return normal_chance * ((0.01 + cth) / 2.0 + 0.49) + wrecking
    return wrecking


_PERFECT_TURRET_MULT = _expected_turret_mult(1.0)   # 1.01505 — the normalisation baseline


def _turret_cth(tracking: float, optimal_sig: float, optimal: float, falloff: float,
                angular: float, target_sig: float, distance: float) -> float:
    """Chance to hit = rangeFactor × trackingFactor (both 0.5**exponent, so exponents add)."""
    if falloff > 0:
        range_exp = (max(0.0, distance - optimal) / falloff) ** 2
    else:
        range_exp = 0.0 if distance <= optimal else math.inf
    if tracking > 0 and target_sig > 0:
        track_exp = ((angular * optimal_sig) / (tracking * target_sig)) ** 2
    else:
        track_exp = 0.0
    return 0.5 ** (range_exp + track_exp)


def _applied_turret_multiplier(cth: float) -> float:
    """Normalised application factor (≤ 1.0 in practice; 1.0 at perfect application)."""
    return _expected_turret_mult(cth) / _PERFECT_TURRET_MULT


def _lock_time_s(scan_res: float, target_sig: float) -> float | None:
    """Time to lock a target of ``target_sig`` (m) at ``scan_res`` mm (scanResolution 564):
    ``40000 / scanRes / asinh(sig)²`` seconds, capped at 30 min (CCP/EVE-Uni targeting
    formula; pyfa eos/calc.py:68-71). ``None`` when either input is missing."""
    if scan_res <= 0 or target_sig <= 0:
        return None
    return min(40000.0 / scan_res / (math.asinh(target_sig) ** 2), 30 * 60.0)


def _warp_time_s(warp_speed_au_s: float, subwarp_speed: float,
                 warp_distance_au: float) -> float | None:
    """Total time in warp over ``warp_distance_au`` AU (CCP dev blog "Warp Drive Active",
    2014 acceleration rework; pyfa graphs/data/fitWarpTime/getter.py:50-77).

    Acceleration k = warp speed (AU/s); deceleration j = min(k/3, 2) AU/s. The ship exits
    warp at min(subwarp/2, 100) m/s (``subwarp`` is the propulsion-off max velocity, since
    prop mods can't run in warp). Accel covers 1 AU, decel covers v_max/j; if the trip is
    shorter than accel+decel the peak speed never reaches v_max (solved from the two
    exponential legs), otherwise the remainder is spent cruising."""
    if warp_speed_au_s <= 0 or warp_distance_au <= 0:
        return None
    dropout = min(subwarp_speed / 2.0, 100.0)
    if dropout <= 0:
        return None
    warp_dist = warp_distance_au * AU_METERS
    k_accel = warp_speed_au_s
    k_decel = min(warp_speed_au_s / 3.0, 2.0)
    max_ms = warp_speed_au_s * AU_METERS
    minimum_dist = AU_METERS + max_ms / k_decel        # accel_dist + decel_dist
    cruise_time = 0.0
    if minimum_dist > warp_dist:
        max_ms = warp_dist * k_accel * k_decel / (k_accel + k_decel)
    else:
        cruise_time = (warp_dist - minimum_dist) / max_ms
    accel_time = math.log(max_ms / k_accel) / k_accel
    decel_time = math.log(max_ms / dropout) / k_decel
    return cruise_time + accel_time + decel_time


# --------------------------------------------------------------------------- #
# Offence
# --------------------------------------------------------------------------- #
def _missile_application(target_sig, target_vel, er, ev_, drf) -> float:
    """min(1, S/Er, ((S/Er)·(Ev/Vt))^DRF) — post-2015 formula: attr 1353
    (aoeDamageReductionFactor) IS the exponent (verified against live data; the old
    ln(drf)/ln(drs) form belongs to the pre-rework attributes)."""
    if er <= 0:
        return 1.0
    size_term = target_sig / er
    if target_vel <= 0 or ev_ <= 0 or drf <= 0:
        return min(1.0, size_term)
    return min(1.0, size_term, (size_term * (ev_ / target_vel)) ** drf)


def _unerr(value: float) -> float:
    """Kill float-division error before flooring a magazine size — ``int(0.4/0.0025)``
    is 159, not 160, without this. Rounds to ~7 significant figures (the standard
    round-to-significant-digits correction)."""
    if value <= 0 or not math.isfinite(value):
        return value
    return round(value, 7 - math.ceil(math.log10(value)))


def _magazine_shots(ev: EvaluatedFit, m: Entity):
    """Shots a weapon fires before it must reload; 0 when it never depletes; None when
    the magazine cannot be determined from the data.

    Semantics mirror EVE's own, studied for BEHAVIOUR ONLY in pyfa's GPL eos
    (saveddata/module.py:196-302 — no code reused):
    * Ammo weapons carry chargeRate (56). The magazine holds
      ``floor(capacity(38) / charge volume(161))`` rounds and fires
      ``floor(rounds / chargeRate)`` shots before reloading. capacity/volume are read
      as BASE attributes (a fit does not modify them); chargeRate is evaluated.
    * Frequency crystals carry no chargeRate; depletion is governed by the CHARGE's
      crystalsGetDamaged (786). ==1 -> the lens wears out after an expected
      ``floor(rounds × hp(9) / (crystalVolatilityDamage(784) × crystalVolatilityChance(783)))``
      shots; otherwise (T1 lenses and every lens the SDE flags 0) it never depletes.
    """
    ch = m.charge
    if ch is None:
        return None
    capacity = m.base.get(ATTR_CAPACITY)
    volume = ch.base.get(ATTR_VOLUME)
    if not capacity or not volume or volume <= 0:
        return None
    rounds = int(_unerr(capacity / volume))
    if rounds <= 0:
        return None
    if ATTR_CHARGE_RATE in m.base:
        rate = ev.value(m, ATTR_CHARGE_RATE)
        return math.floor(rounds / rate) if rate > 0 else None
    if ATTR_CRYSTALS_GET_DAMAGED in ch.base:
        if ev.value(ch, ATTR_CRYSTALS_GET_DAMAGED) != 1:
            return 0                        # permanent crystal — never reloads
        hp = ev.value(ch, ATTR_CHARGE_HP) if ATTR_CHARGE_HP in ch.base else 0.0
        chance = (ev.value(ch, ATTR_CRYSTAL_VOL_CHANCE)
                  if ATTR_CRYSTAL_VOL_CHANCE in ch.base else 0.0)
        dmg = (ev.value(ch, ATTR_CRYSTAL_VOL_DAMAGE)
               if ATTR_CRYSTAL_VOL_DAMAGE in ch.base else 0.0)
        denom = dmg * chance
        if hp <= 0 or denom <= 0:
            return None
        return math.floor(rounds * hp / denom)
    return 0                                # no depletion mechanism — non-depleting


def _sustained_entry(ev: EvaluatedFit, m: Entity, volley: float, dps: float,
                     rof_s: float):
    """Reload-aware sustained DPS for one weapon.

    Returns ``(telemetry fields, dps for the fit's sustained total)`` where the dps is
    None when it cannot be computed. A finite magazine of N shots fires for
    ``N × cycle`` then pays reloadTime(1795) before firing again, so the long-run rate
    is ``magazine_damage / (time_to_empty + reload)`` — strictly below burst. A
    non-depleting weapon (permanent crystal) never reloads, so sustained == burst; its
    magazine/time/reload fields are null to say so. When the magazine is indeterminate
    (missing/zero capacity or charge volume) the sustained figure is null with a reason
    rather than a computed-looking zero.
    """
    shots = _magazine_shots(ev, m)
    if shots is None:
        return {"sustained_dps": None,
                "sustained_reason": "magazine_indeterminate"}, None
    if shots == 0:
        return {"magazine_shots": None, "time_to_empty_s": None, "reload_s": None,
                "sustained_dps": round(dps, 1)}, dps
    reload_s = ev.value(m, ATTR_RELOAD_TIME) / 1000.0
    time_to_empty_s = shots * rof_s
    span = time_to_empty_s + reload_s
    sustained = (shots * volley) / span if span > 0 else 0.0
    return {"magazine_shots": shots,
            "time_to_empty_s": round(time_to_empty_s, 1),
            "reload_s": round(reload_s, 1),
            "sustained_dps": round(sustained, 1)}, sustained


def _offence(ev: EvaluatedFit, provider, op_profile: OperatingProfile, result) -> dict:
    turret_dps = missile_dps = missile_dps_applied = drone_dps = total_volley = 0.0
    turret_dps_applied = drone_dps_applied = 0.0
    sustained_weapon_dps = 0.0
    damage_by_type = {d: 0.0 for d in A.DAMAGE_TYPES}
    weapons = 0
    ranges: list[dict] = []
    target = op_profile.target
    # Turret/drone application needs a distance (range term) and an angular speed (tracking
    # term). Missiles need only sig/velocity, so they apply whenever a target is set.
    angular = target.effective_angular() if target is not None else None
    turret_complete = bool(
        target is not None and target.signature_radius > 0
        and target.target_distance_m is not None and target.target_distance_m > 0
        and angular is not None)
    applied_complete = True

    for m in ev.modules:
        if m.slot != SlotKind.HIGH or m.module_state == ModuleState.OFFLINE:
            continue
        is_turret = bool(A.TURRET_EFFECTS & m.effect_ids)
        is_launcher = A.EFFECT_LAUNCHER in m.effect_ids
        if not (is_turret or is_launcher):
            continue
        weapons += 1
        if not _active(m):
            continue                          # online weapons don't fire
        if m.charge is None:
            result.diagnostics.append(Diagnostic(
                "missing_ammo", Severity.WARNING, "Weapon has no charge loaded",
                detail=f"type {m.type_id}", suggested_action="Load a compatible charge.",
                contextual=False, params={"type_id": m.type_id}))
            continue
        ch = m.charge
        shot = {d: ev.value(ch, A.CHARGE_DAMAGE[d]) if A.CHARGE_DAMAGE[d] in ch.base
                else 0.0 for d in A.DAMAGE_TYPES}
        shot_total = sum(shot.values())
        dmg_mult = ev.value(m, A.DAMAGE_MULTIPLIER) if is_turret else 1.0
        rof_s = ev.value(m, A.RATE_OF_FIRE) / 1000.0
        if rof_s <= 0 or shot_total <= 0:
            continue
        volley = shot_total * dmg_mult
        dps = volley / rof_s
        entry = {"type_id": m.type_id, "volley": round(volley, 1), "dps": round(dps, 1)}
        if is_turret:
            turret_dps += dps
            entry.update(
                kind="turret",
                optimal_m=round(ev.value(m, A.OPTIMAL_RANGE), 0),
                falloff_m=round(ev.value(m, A.FALLOFF), 0),
                tracking=round(ev.value(m, A.TRACKING_SPEED), 4),
            )
            if target is not None:
                if turret_complete:
                    cth = _turret_cth(
                        ev.value(m, A.TRACKING_SPEED),
                        ev.value(m, A.OPTIMAL_SIG_RADIUS),
                        ev.value(m, A.OPTIMAL_RANGE), ev.value(m, A.FALLOFF),
                        angular, target.signature_radius, target.target_distance_m)
                    amult = _applied_turret_multiplier(cth)
                    entry["applied_dps"] = round(dps * amult, 1)
                    entry["applied_multiplier"] = round(amult, 3)
                    turret_dps_applied += dps * amult
                else:
                    entry["applied_dps"] = None
                    entry["applied_reason"] = "target_profile_incomplete"
                    applied_complete = False
        else:
            missile_dps += dps
            vel = ev.value(ch, ATTR_MISSILE_VELOCITY)
            flight_s = ev.value(ch, ATTR_EXPLOSION_DELAY) / 1000.0
            entry.update(
                kind="missile",
                missile_velocity=round(vel, 0),
                flight_time_s=round(flight_s, 1),
                range_m=round(vel * flight_s, 0),
            )
            applied = dps
            if target is not None:
                factor = _missile_application(
                    target.signature_radius, target.velocity,
                    ev.value(ch, A.AOE_CLOUD_SIZE), ev.value(ch, A.AOE_VELOCITY),
                    ev.value(ch, A.AOE_DAMAGE_REDUCTION_FACTOR))
                applied = dps * factor
                entry["applied_dps"] = round(applied, 1)
                entry["applied_multiplier"] = round(factor, 3)
            missile_dps_applied += applied
        sustained_fields, sustained_dps = _sustained_entry(ev, m, volley, dps, rof_s)
        entry.update(sustained_fields)
        if sustained_dps is not None:
            sustained_weapon_dps += sustained_dps
        total_volley += volley
        ranges.append(entry)
        for d in A.DAMAGE_TYPES:
            damage_by_type[d] += (shot[d] * dmg_mult) / rof_s

    # Drones: only the set launchable within bandwidth adds DPS (greedy in fitted
    # order — the pilot's stated active set, truncated when bandwidth runs out).
    bandwidth = ev.ship_value(A.DRONE_BANDWIDTH)
    bw_left = bandwidth
    for dr in ev.drones:
        used = ev.value(dr, ATTR_DRONE_BANDWIDTH_USED)
        qty_launchable = dr.quantity if used <= 0 else min(
            dr.quantity, int(bw_left // used) if used > 0 else dr.quantity)
        if qty_launchable < dr.quantity:
            result.diagnostics.append(Diagnostic(
                "drones_over_bandwidth", Severity.WARNING,
                "Not all drones fit in bandwidth",
                detail=f"type {dr.type_id}: {qty_launchable} of {dr.quantity} counted",
                contextual=False,
                params={"type_id": dr.type_id, "counted": qty_launchable,
                        "requested": dr.quantity}))
        bw_left -= used * qty_launchable
        shot = {d: ev.value(dr, A.CHARGE_DAMAGE[d]) if A.CHARGE_DAMAGE[d] in dr.base
                else 0.0 for d in A.DAMAGE_TYPES}
        shot_total = sum(shot.values())
        mult = ev.value(dr, A.DAMAGE_MULTIPLIER) if A.DAMAGE_MULTIPLIER in dr.base else 1.0
        rof_s = ev.value(dr, A.RATE_OF_FIRE) / 1000.0 if A.RATE_OF_FIRE in dr.base else 0.0
        if shot_total > 0 and rof_s > 0 and qty_launchable > 0:
            d_dps = (shot_total * mult) / rof_s * qty_launchable
            drone_dps += d_dps
            for d in A.DAMAGE_TYPES:
                damage_by_type[d] += (shot[d] * mult) / rof_s * qty_launchable
            if target is not None:
                d_applied = _drone_applied(ev, dr, d_dps, target, angular,
                                           turret_complete)
                if d_applied is None:
                    applied_complete = False
                else:
                    drone_dps_applied += d_applied

    total = turret_dps + missile_dps + drone_dps
    dist = ({d: round(damage_by_type[d] / total * 100, 1) for d in A.DAMAGE_TYPES}
            if total > 0 else {d: 0.0 for d in A.DAMAGE_TYPES})
    result.traces["dps"] = AttributeTrace(
        "dps", 0.0, round(total, 1), "dps",
        [Contribution("Turrets", "module", f"{turret_dps:.1f} dps"),
         Contribution("Missiles", "module", f"{missile_dps:.1f} dps"),
         Contribution("Drones", "module", f"{drone_dps:.1f} dps")])
    if weapons == 0 and drone_dps == 0:
        result.unsupported.append("no_weapons_detected")
    out = {
        "turret_dps": round(turret_dps, 1), "missile_dps": round(missile_dps, 1),
        "drone_dps": round(drone_dps, 1),
        "total_dps": round(total, 1), "volley": round(total_volley, 1),
        # Drones fire continuously (no magazine), so they sustain by definition.
        "total_sustained_dps": round(sustained_weapon_dps + drone_dps, 1),
        "damage_distribution": dist,
        "weapons": ranges,
    }
    if target is not None:
        out["missile_dps_applied"] = round(missile_dps_applied, 1)
        out["missile_application"] = (round(missile_dps_applied / missile_dps, 3)
                                      if missile_dps > 0 else None)
        out["turret_dps_applied"] = round(turret_dps_applied, 1)
        out["turret_application"] = (round(turret_dps_applied / turret_dps, 3)
                                     if turret_dps > 0 else None)
        out["drone_dps_applied"] = round(drone_dps_applied, 1)
        out["drone_application"] = (round(drone_dps_applied / drone_dps, 3)
                                    if drone_dps > 0 else None)
        # Applied total sums only the classes we could compute; a weapon excluded because
        # the target profile was incomplete flips applied_complete to False (never faked).
        out["total_applied_dps"] = round(
            turret_dps_applied + missile_dps_applied + drone_dps_applied, 1)
        out["applied_complete"] = applied_complete
        # Legacy field (pre-WS-2): only missiles applied, turrets/drones raw. Kept so older
        # saved renders never KeyError; superseded by total_applied_dps above.
        out["applied_total_dps"] = round(
            turret_dps + missile_dps_applied + drone_dps, 1)
        out["target"] = {"signature_radius": target.signature_radius,
                         "velocity": target.velocity, "label": target.label,
                         "distance_m": target.target_distance_m, "angular": angular}
    return out


def _drone_applied(ev: EvaluatedFit, dr: Entity, d_dps: float, target, angular,
                   turret_complete: bool) -> float | None:
    """Applied DPS for one drone entity, or ``None`` when the target profile is too
    incomplete to compute it (same honesty as the turret path).

    Model (pyfa getDroneMult, application.py:273-314, studied not copied): a mobile drone
    at least as fast as the target chases it down and applies in full (cth = 1); a sentry
    (maxVelocity≈0) or a mobile drone slower than the target is treated as a stationary
    turret tracking the target's transversal. We omit ship/drone radii and treat the
    attacker as stationary — the conservative simplification, matching pyfa's stationary-
    attacker behaviour where a slow drone at ship centre tracks the full target angular."""
    max_vel = ev.value(dr, A.MAX_VELOCITY) if A.MAX_VELOCITY in dr.base else 0.0
    if max_vel > _DRONE_MOBILE_MIN_SPEED and max_vel >= target.velocity:
        return d_dps
    if not turret_complete:
        return None
    cth = _turret_cth(
        ev.value(dr, A.TRACKING_SPEED) if A.TRACKING_SPEED in dr.base else 0.0,
        ev.value(dr, A.OPTIMAL_SIG_RADIUS) if A.OPTIMAL_SIG_RADIUS in dr.base else 0.0,
        ev.value(dr, A.OPTIMAL_RANGE) if A.OPTIMAL_RANGE in dr.base else 0.0,
        ev.value(dr, A.FALLOFF) if A.FALLOFF in dr.base else 0.0,
        angular, target.signature_radius, target.target_distance_m)
    return d_dps * _applied_turret_multiplier(cth)


# --------------------------------------------------------------------------- #
# Mobility / targeting / EWAR
# --------------------------------------------------------------------------- #
_LN_ALIGN = -math.log(0.25)


def _mobility(ev: EvaluatedFit, op_profile: OperatingProfile, result) -> dict:
    base_v = ev.ship_value(A.MAX_VELOCITY)   # graph applies overdrives/nanos/skills
    agility = ev.ship_value(A.AGILITY)
    mass = ev.ship_value(A.MASS)             # plates' massAddition arrives via modAdd
    sig = ev.ship_value(A.SIGNATURE_RADIUS)

    prop_v = base_v
    if op_profile.propulsion_active:
        mwd_sig_role = ev.ship.base.get(A.MWD_SIG_ROLE_BONUS, 0.0)
        for m in ev.modules:
            if not _active(m) or m.group_id not in (46, 47):
                continue
            mass += ev.value(m, A.MASS_ADDITION) if A.MASS_ADDITION in m.base else 0.0
            sf = ev.value(m, A.SPEED_BONUS)
            thrust = ev.value(m, A.SPEED_BOOST_FACTOR) if A.SPEED_BOOST_FACTOR in m.base \
                else 0.0
            if sf and thrust and mass > 0:
                prop_v = base_v * (1.0 + (sf / 100.0) * (thrust / mass))
            elif sf:
                prop_v = base_v * (1.0 + sf / 100.0)
                result.unsupported.append("prop_velocity_approx_no_mass")
            sig_bonus = ev.value(m, A.SIGNATURE_RADIUS_BONUS) \
                if A.SIGNATURE_RADIUS_BONUS in m.base else 0.0
            if sig_bonus:
                sig *= 1.0 + (sig_bonus / 100.0) * (1.0 + mwd_sig_role / 100.0)
            break
    align = _LN_ALIGN * mass * agility / 1_000_000.0 if mass and agility else 0.0
    # Warp-time estimate over the requested distance. Warp speed (AU/s) = baseWarpSpeed ×
    # warpSpeedMultiplier; subwarp = the propulsion-off max velocity (prop can't run in
    # warp), which is exactly base_v here since prop is applied only to prop_v above.
    warp_speed_au = ev.ship_value(A.WARP_SPEED_MULT) * (
        ev.ship.base.get(A.BASE_WARP_SPEED, 1.0) or 1.0)
    warp_time = _warp_time_s(warp_speed_au, base_v, op_profile.warp_distance_au)
    return {
        "max_velocity": round(base_v, 1),
        "propulsion_velocity": round(prop_v, 1),
        "align_time_s": round(align, 2),
        "mass": round(mass, 0),
        "agility": round(agility, 4),
        "signature_radius": round(sig, 1),
        "warp_speed": round(ev.ship_value(A.WARP_SPEED_MULT), 2),
        "warp_distance_au": round(op_profile.warp_distance_au, 1),
        "warp_time_s": round(warp_time, 1) if warp_time is not None else None,
    }


def _targeting(ev: EvaluatedFit, op_profile: OperatingProfile) -> dict:
    sensors = {k: ev.ship_value(v) for k, v in A.SENSOR_STRENGTHS.items()}
    strongest = max(sensors.items(), key=lambda kv: kv[1]) if sensors else ("", 0.0)
    scan_res = ev.ship_value(A.SCAN_RESOLUTION)
    lock_time = None
    if op_profile.target is not None and op_profile.target.signature_radius > 0:
        lock_time = _lock_time_s(scan_res, op_profile.target.signature_radius)
    return {
        "max_target_range": round(ev.ship_value(A.MAX_TARGET_RANGE), 0),
        "max_locked_targets": int(ev.ship_value(A.MAX_LOCKED_TARGETS)),
        "scan_resolution": round(scan_res, 0),
        "sensor_strength": round(strongest[1], 1),
        "sensor_type": strongest[0],
        "lock_time_s": round(lock_time, 2) if lock_time is not None else None,
    }


def _ewar(ev: EvaluatedFit) -> dict:
    from .dogma import (
        EWAR_ECM,
        EWAR_ENERGY_NEUT,
        EWAR_ENERGY_NOS,
        EWAR_GROUPS,
        EWAR_SENSOR_DAMP,
        EWAR_STASIS_WEB,
        EWAR_TARGET_PAINTER,
        EWAR_WARP_SCRAMBLER,
        EWAR_WEAPON_DISRUPTOR,
    )
    entries: list[dict] = []
    for m in ev.modules:
        gid = m.group_id
        if gid not in EWAR_GROUPS or not _active(m):
            continue
        e = {"type_id": m.type_id, "group_id": gid,
             "optimal_m": round(ev.value(m, A.OPTIMAL_RANGE), 0),
             "falloff_m": round(ev.value(m, A.FALLOFF), 0)}
        if gid == EWAR_WARP_SCRAMBLER:
            e.update(kind="warp_disruption",
                     strength=round(ev.value(m, A.WARP_SCRAMBLE_STRENGTH), 1),
                     unit="points")
        elif gid == EWAR_STASIS_WEB:
            e.update(kind="stasis_web",
                     strength=round(abs(ev.value(m, A.SPEED_BONUS)), 1), unit="% speed")
        elif gid in (EWAR_ENERGY_NEUT, EWAR_ENERGY_NOS):
            is_neut = gid == EWAR_ENERGY_NEUT
            amt = ev.value(m, A.ENERGY_NEUTRALISER_AMOUNT if is_neut
                           else A.POWER_TRANSFER_AMOUNT)
            cyc = _cycle_ms(ev, m) / 1000.0
            e.update(kind="energy_neutraliser" if is_neut else "nosferatu",
                     strength=round(amt, 1), unit="GJ/cycle",
                     per_second=round(amt / cyc, 1) if cyc > 0 else 0.0)
        elif gid == EWAR_TARGET_PAINTER:
            e.update(kind="target_painter",
                     strength=round(ev.value(m, A.SIGNATURE_RADIUS_BONUS_ATTR), 1),
                     unit="% sig")
        elif gid == EWAR_SENSOR_DAMP:
            e.update(kind="sensor_dampener", unit="%",
                     lock_range_bonus=round(ev.value(m, A.MAX_TARGET_RANGE_BONUS), 1),
                     scan_res_bonus=round(ev.value(m, A.SCAN_RESOLUTION_BONUS), 1))
        elif gid == EWAR_ECM:
            strengths = {k: ev.value(m, v) for k, v in A.ECM_STRENGTH.items()}
            best = max(strengths.items(), key=lambda kv: kv[1]) if strengths else ("", 0.0)
            e.update(kind="ecm", strength=round(best[1], 1), unit="points",
                     jam_type=best[0],
                     jam_strengths={k: round(v, 1) for k, v in strengths.items()})
        elif gid == EWAR_WEAPON_DISRUPTOR:
            # Report the disruptor's own tracking/range disruption attributes (the
            # strongest of its scripted outputs; scripts modify these via the graph).
            e.update(kind="weapon_disruptor", unit="%",
                     tracking_bonus=round(ev.value(m, 1255) if 1255 in m.base else 0.0, 1),
                     optimal_bonus=round(ev.value(m, 351) if 351 in m.base else 0.0, 1),
                     falloff_bonus=round(ev.value(m, 349) if 349 in m.base else 0.0, 1))
        entries.append(e)
    return {"modules": entries, "count": len(entries)}


# --------------------------------------------------------------------------- #
# Skills + status
# --------------------------------------------------------------------------- #
def _missing_skills(fit: FitInput, skills: SkillProfile, provider) -> list[MissingSkill]:
    missing: list[MissingSkill] = []
    seen: set[tuple[int, int]] = set()
    type_ids = {fit.ship_type_id}
    for m in fit.modules:
        type_ids.add(m.type_id)
        if m.charge_type_id:
            type_ids.add(m.charge_type_id)
    for tid in type_ids:
        for skill_id, level in provider.required_skills(tid):
            key = (skill_id, tid)
            if key in seen:
                continue
            seen.add(key)
            have = skills.level(skill_id)
            if have < level:
                missing.append(MissingSkill(skill_id, level, have, tid))
    return missing


def _finalise_status(result: FittingResult) -> None:
    codes = {d.code for d in result.diagnostics if d.severity == Severity.ERROR}
    structural = {"too_many_modules", "turret_hardpoints", "launcher_hardpoints",
                  "rig_size_mismatch", "max_group_fitted", "incompatible_charge",
                  "charge_size_mismatch", "drone_bay_exceeded",
                  "ship_restriction_violated", "max_group_active_exceeded",
                  "max_group_online_exceeded", "subsystem_slot_conflict",
                  "subsystem_count_invalid", "implant_slot_conflict",
                  "booster_slot_conflict", "mode_invalid_for_ship"}
    resource = {"cpu_exceeded", "powergrid_exceeded", "calibration_exceeded",
                "drone_bandwidth_exceeded"}
    if codes & structural:
        result.status = Status.IMPOSSIBLE
    elif codes & resource:
        result.status = Status.OVER_RESOURCES
    elif result.missing_skills:
        result.status = Status.MISSING_SKILLS
    elif result.diagnostics:
        result.status = Status.WARNINGS
    else:
        result.status = Status.VALID
