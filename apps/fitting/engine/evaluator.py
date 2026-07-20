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
ATTR_MISSILE_VELOCITY = 37
ATTR_EXPLOSION_DELAY = 281
ATTR_SHIELD_RECHARGE = 479
ATTR_SHIELD_BOOST = 68
ATTR_ARMOR_REPAIR = 84
ATTR_HULL_REPAIR = 87           # structureDamageAmount (hull repairers)
ATTR_DURATION = 73
_RECHARGE_PEAK = 2.5            # peak dC/dt = 2.5·Cmax/τ, at 25% charge

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
    targeting = _targeting(ev)
    utility = {"cargo": round(ev.ship_value(ATTR_CAPACITY), 1),
               "drone_bay": round(ev.ship_value(A.DRONE_CAPACITY), 1)}
    ewar = _ewar(ev)

    result.telemetry = {
        "resources": resources, "defence": defence, "capacitor": capacitor,
        "offence": offence, "mobility": mobility, "targeting": targeting,
        "utility": utility, "ewar": ewar,
        "ship": {"type_id": fit.ship_type_id, "name": ship_info.get("name", "")},
        "operating_profile": {
            "mode": op_profile.mode.value,
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
            if A.EFFECT_TURRET in m.effect_ids or (
                    m.module_state == ModuleState.OFFLINE
                    and A.EFFECT_TURRET in provider.effects(m.type_id)):
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


def _offence(ev: EvaluatedFit, provider, op_profile: OperatingProfile, result) -> dict:
    turret_dps = missile_dps = missile_dps_applied = drone_dps = total_volley = 0.0
    damage_by_type = {d: 0.0 for d in A.DAMAGE_TYPES}
    weapons = has_turret = 0
    ranges: list[dict] = []
    target = op_profile.target

    for m in ev.modules:
        if m.slot != SlotKind.HIGH or m.module_state == ModuleState.OFFLINE:
            continue
        is_turret = A.EFFECT_TURRET in m.effect_ids
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
            has_turret = 1
            turret_dps += dps
            entry.update(
                kind="turret",
                optimal_m=round(ev.value(m, A.OPTIMAL_RANGE), 0),
                falloff_m=round(ev.value(m, A.FALLOFF), 0),
                tracking=round(ev.value(m, A.TRACKING_SPEED), 4),
            )
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
                applied = dps * _missile_application(
                    target.signature_radius, target.velocity,
                    ev.value(ch, A.AOE_CLOUD_SIZE), ev.value(ch, A.AOE_VELOCITY),
                    ev.value(ch, A.AOE_DAMAGE_REDUCTION_FACTOR))
            missile_dps_applied += applied
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
        "damage_distribution": dist,
        "weapons": ranges,
    }
    if target is not None:
        out["missile_dps_applied"] = round(missile_dps_applied, 1)
        out["missile_application"] = (round(missile_dps_applied / missile_dps, 3)
                                      if missile_dps > 0 else None)
        out["applied_total_dps"] = round(
            turret_dps + missile_dps_applied + drone_dps, 1)
        out["target"] = {"signature_radius": target.signature_radius,
                         "velocity": target.velocity, "label": target.label}
        if has_turret:
            result.unsupported.append("turret_application_not_modelled")
    return out


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
    return {
        "max_velocity": round(base_v, 1),
        "propulsion_velocity": round(prop_v, 1),
        "align_time_s": round(align, 2),
        "mass": round(mass, 0),
        "agility": round(agility, 4),
        "signature_radius": round(sig, 1),
        "warp_speed": round(ev.ship_value(A.WARP_SPEED_MULT), 2),
    }


def _targeting(ev: EvaluatedFit) -> dict:
    sensors = {k: ev.ship_value(v) for k, v in A.SENSOR_STRENGTHS.items()}
    strongest = max(sensors.items(), key=lambda kv: kv[1]) if sensors else ("", 0.0)
    return {
        "max_target_range": round(ev.ship_value(A.MAX_TARGET_RANGE), 0),
        "max_locked_targets": int(ev.ship_value(A.MAX_LOCKED_TARGETS)),
        "scan_resolution": round(ev.ship_value(A.SCAN_RESOLUTION), 0),
        "sensor_strength": round(strongest[1], 1),
        "sensor_type": strongest[0],
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
                  "charge_size_mismatch", "drone_bay_exceeded"}
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
