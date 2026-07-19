"""The independent dogma evaluator.

Given a fit, a skill profile and an operating profile, it computes ship telemetry
(fitting resources, defence, offence, capacitor, mobility, targeting, utility),
diagnostics and explainability traces — deterministically, from base attributes plus
publicly documented EVE mechanics (stacking penalty, ship/role/skill bonuses,
resonance-based EHP, turret/drone DPS, capacitor recharge). It reads all data through a
:class:`DataProvider` so it never touches the ORM, ESI, the request or the network.

This is an original implementation. Where a mechanic is not modelled it is reported in
``FittingResult.unsupported`` rather than approximated silently.
"""
from __future__ import annotations

import math
from time import perf_counter
from typing import Protocol

from . import attributes as A
from .bonuses import BonusContext, BonusSpec
from .stacking import combine_penalized, combine_unpenalized
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

# Launcher/turret group ids (public SDE inventory groups) used for hardpoint accounting.
LAUNCHER_GROUPS = frozenset({507, 508, 509, 510, 511, 524, 771, 1245, 1246})
TURRET_GROUPS = frozenset({53, 55, 74})
# Damage-mod groups per weapon class, so a Ballistic Control boosts only missiles and a
# gyrostabiliser only turrets (they never cross-boost).
TURRET_DAMAGE_MOD_GROUPS = frozenset({59, 326, 327})     # gyrostabilizer(59) / magstab / heat sink
LAUNCHER_DAMAGE_MOD_GROUPS = frozenset({367})            # ballistic control system
DRONE_DAMAGE_MOD_GROUPS = frozenset({640})               # drone damage amplifier
SHIELD_EXTENDER_GROUP = 40
ARMOR_PLATE_GROUP = 329  # armor reinforcer / plates (flat armour HP)
PROP_GROUPS = frozenset({46, 47})  # afterburner, MWD
# Slots whose modules are physically fitted to the HULL and so contribute to the ship's own HP
# and resonance. Drones/fighters/cargo/implants are separate entities that carry their OWN hp
# and resonance attributes — those must never leak into the ship's layers.
HULL_FITTED_SLOTS = frozenset({
    SlotKind.HIGH, SlotKind.MED, SlotKind.LOW, SlotKind.RIG, SlotKind.SUBSYSTEM,
})

# Electronic-warfare inventory groups (public SDE group ids), used for the EWAR readout.
EWAR_ECM = 201
EWAR_SENSOR_DAMP = 208
EWAR_TARGET_PAINTER = 209
EWAR_WEAPON_DISRUPTOR = 213
EWAR_STASIS_WEB = 65
EWAR_WARP_SCRAMBLER = 52
EWAR_ENERGY_NEUT = 71
EWAR_ENERGY_NOS = 68
EWAR_GROUPS = frozenset({
    EWAR_ECM, EWAR_SENSOR_DAMP, EWAR_TARGET_PAINTER, EWAR_WEAPON_DISRUPTOR,
    EWAR_STASIS_WEB, EWAR_WARP_SCRAMBLER, EWAR_ENERGY_NEUT, EWAR_ENERGY_NOS,
})

_LN_10_PCT = -math.log(0.25)  # 1.386294… — the align-time / e-folding constant


def missile_application(target_sig: float, target_vel: float, explosion_radius: float,
                        explosion_velocity: float, drf: float, drs: float) -> float:
    """Fraction (0..1) of a missile's damage applied to a target of the given signature
    radius and velocity — the standard EVE missile application formula:

        min(1, S/Er, ((S/Er)·(Ev/Vt))^(ln(DRF)/ln(DRS)))

    with S = target signature, Er/Ev the missile's explosion radius/velocity, and DRF/DRS
    the charge's ``aoeDamageReductionFactor``/``aoeDamageReductionSensitivity``. A target no
    faster than the explosion velocity (or stationary) takes full, signature-limited damage.
    Degrades to the size term when the reduction attributes are absent."""
    if explosion_radius <= 0:
        return 1.0
    size_term = target_sig / explosion_radius
    if (target_vel <= 0 or explosion_velocity <= 0
            or drf <= 0 or drs <= 0 or drs == 1.0):
        return min(1.0, size_term)
    exponent = math.log(drf) / math.log(drs)
    speed_term = (size_term * (explosion_velocity / target_vel)) ** exponent
    return min(1.0, size_term, speed_term)


class DataProvider(Protocol):
    data_version: str

    def type_info(self, type_id: int) -> dict | None: ...
    def attrs(self, type_id: int) -> dict[int, float]: ...
    def effects(self, type_id: int) -> frozenset[int]: ...
    def required_skills(self, type_id: int) -> list[tuple[int, int]]: ...
    def ship_bonuses(self, ship_type_id: int) -> list[BonusSpec]: ...


# --------------------------------------------------------------------------- #
# Bonus application helpers
# --------------------------------------------------------------------------- #
def _matches(spec: BonusSpec, info: dict, item_attrs: dict[int, float]) -> bool:
    if spec.match_group_ids and info.get("group_id") not in spec.match_group_ids:
        return False
    if spec.match_category_ids and info.get("category_id") not in spec.match_category_ids:
        return False
    if spec.match_attr_present is not None and spec.match_attr_present not in item_attrs:
        return False
    if spec.match_effect_id is not None and spec.match_effect_id not in info.get("effects", ()):
        return False
    if spec.match_required_skill_id is not None \
            and spec.match_required_skill_id not in info.get("req_skills", ()):
        return False
    return True


def _weapon_kind(info: dict) -> str | None:
    """Classify a module as a weapon by its dogma effects — 'turret', 'launcher' or None.
    Effect-based so every launcher group (cruise, rapid, XL, …) is recognised, not just an
    enumerated few."""
    effects = info.get("effects", ())
    if A.EFFECT_LAUNCHER in effects:
        return "launcher"
    if A.EFFECT_TURRET in effects:
        return "turret"
    return None


def _bonus_factor_for_item(
    ctx: BonusContext, skills: SkillProfile, attr_id: int, info: dict, item_attrs: dict[int, float],
    domain: str = "item",
) -> tuple[float, list[Contribution]]:
    """Combined UNPENALISED factor from ship/role/skill bonuses on an item/charge attribute.

    ``domain`` selects which bonuses apply: "item" (a fitted module's attribute) or "charge"
    (the loaded ammo's attribute — e.g. a hull's per-type missile-damage bonus lands on the
    missile, not the launcher)."""
    factors: list[float] = []
    contribs: list[Contribution] = []
    for spec in ctx.all():
        if spec.target_domain != domain or spec.target_attr != attr_id or spec.penalised:
            continue
        if not _matches(spec, info, item_attrs):
            continue
        level = skills.level(spec.skill_id) if spec.skill_id else 1
        if spec.skill_id and level <= 0:
            continue
        f = spec.factor(level)
        if f != 1.0:
            factors.append(f)
            kind = "skill" if spec.skill_id else "ship_bonus"
            contribs.append(Contribution(spec.label or spec.key, kind, f"×{f:.4f}", None))
    return combine_unpenalized(factors), contribs


def _ship_attr(
    ship: dict[int, float], attr_id: int, ctx: BonusContext, skills: SkillProfile,
    *, default: float = 0.0, trace: AttributeTrace | None = None,
) -> float:
    """A ship attribute with its unpenalised ship/skill bonuses applied."""
    base = ship.get(attr_id, default)
    factors: list[float] = []
    for spec in ctx.all():
        if spec.target_domain != "ship" or spec.target_attr != attr_id or spec.penalised:
            continue
        level = skills.level(spec.skill_id) if spec.skill_id else 1
        if spec.skill_id and level <= 0:
            continue
        f = spec.factor(level)
        if f != 1.0:
            factors.append(f)
            if trace is not None:
                kind = "skill" if spec.skill_id else "ship_bonus"
                trace.contributions.append(Contribution(spec.label or spec.key, kind, f"×{f:.4f}"))
    return base * combine_unpenalized(factors)


# --------------------------------------------------------------------------- #
# T3 strategic-cruiser subsystem assembly
# --------------------------------------------------------------------------- #
# A fitted subsystem ADDS each of these to the hull (ship attr <- subsystem source attr).
_SUBSYSTEM_ADD = {
    A.CPU_OUTPUT: A.CPU_OUTPUT, A.POWER_OUTPUT: A.POWER_OUTPUT,
    A.HI_SLOTS: A.SUB_HI_SLOT_MOD, A.MED_SLOTS: A.SUB_MED_SLOT_MOD, A.LOW_SLOTS: A.SUB_LOW_SLOT_MOD,
    A.TURRET_HARDPOINTS: A.SUB_TURRET_HP_MOD, A.LAUNCHER_HARDPOINTS: A.SUB_LAUNCHER_HP_MOD,
    A.HULL_HP: A.SUB_STRUCTURE_HP_ADD,
    A.DRONE_BANDWIDTH: A.SUB_DRONE_BANDWIDTH_ADD, A.DRONE_CAPACITY: A.SUB_DRONE_CAPACITY_ADD,
}


def _assemble_subsystems(ship, items, ctx, provider):
    """Fold every fitted subsystem's stat additions into the hull and append its bonuses to
    the context. A strategic cruiser is the hull + its subsystems; without this the engine
    sees only the bare hull (too little CPU/PG, too few slots, no structure HP or bonuses)."""
    subs = [(m, a) for m, a, _i in items
            if m.slot == SlotKind.SUBSYSTEM and m.state != ModuleState.OFFLINE]
    if not subs:
        return ship
    ship = dict(ship)
    for m, a in subs:
        for ship_attr, sub_attr in _SUBSYSTEM_ADD.items():
            add = a.get(sub_attr, 0.0)
            if add:
                ship[ship_attr] = ship.get(ship_attr, 0.0) + add
        ctx.ship_bonuses.extend(provider.ship_bonuses(m.type_id))
    return ship


# --------------------------------------------------------------------------- #
# Main entry
# --------------------------------------------------------------------------- #
def evaluate(
    fit: FitInput, skills: SkillProfile, op_profile: OperatingProfile, provider: DataProvider
) -> FittingResult:
    t0 = perf_counter()
    result = FittingResult(status=Status.VALID, data_version=getattr(provider, "data_version", ""))

    ship_info = provider.type_info(fit.ship_type_id)
    ship = provider.attrs(fit.ship_type_id)
    if not ship_info or not ship:
        result.status = Status.IMPOSSIBLE
        result.errors.append("unknown_ship")
        result.compute_ms = (perf_counter() - t0) * 1000
        return result

    # Copy the bonus list (subsystems extend it below) so the provider's cache isn't mutated.
    ctx = BonusContext(ship_bonuses=list(provider.ship_bonuses(fit.ship_type_id)))

    # Resolve every fitted item once. ``info`` carries the module's dogma effects so weapon
    # detection (turret/launcher) is effect-based, not tied to a hand-listed set of groups.
    items: list[tuple] = []  # (module_input, attrs, info)
    for m in fit.modules:
        info = dict(provider.type_info(m.type_id) or {})
        info["effects"] = frozenset(provider.effects(m.type_id))
        # Required skills let a hull bonus filter on "requires skill X" (how EVE scopes most
        # turret/missile bonuses), not just group/category.
        info["req_skills"] = frozenset(sid for sid, _ in provider.required_skills(m.type_id))
        items.append((m, provider.attrs(m.type_id), info))

    # T3 strategic-cruiser subsystems ASSEMBLE the ship: each adds CPU/PG output, slots,
    # hardpoints, structure HP and drone bay to the bare hull, and contributes its own
    # per-subsystem-skill bonuses. Fold both in before any calculation runs.
    ship = _assemble_subsystems(ship, items, ctx, provider)

    def active(m) -> bool:
        return m.state in (ModuleState.ACTIVE, ModuleState.OVERHEATED)

    resources = _resources(ship, items, ctx, skills, result)
    defence = _defence(ship, items, ctx, skills, op_profile, result)
    capacitor = _capacitor(ship, items, ctx, skills, active)
    offence = _offence(items, provider, ctx, skills, op_profile, result, active)
    mobility = _mobility(ship, items, ctx, skills, op_profile, result, active)
    targeting = _targeting(ship, ctx, skills)
    utility = _utility(ship, items)
    ewar = _ewar(items, active)

    result.telemetry = {
        "resources": resources,
        "defence": defence,
        "capacitor": capacitor,
        "offence": offence,
        "mobility": mobility,
        "targeting": targeting,
        "utility": utility,
        "ewar": ewar,
        "ship": {"type_id": fit.ship_type_id, "name": ship_info.get("name", "")},
        "operating_profile": {
            "mode": op_profile.mode.value,
            "propulsion_active": op_profile.propulsion_active,
            "damage_profile": op_profile.damage_profile.normalised().as_map(),
        },
    }

    result.missing_skills = _missing_skills(fit, skills, provider)
    _finalise_status(result, resources)
    result.compute_ms = (perf_counter() - t0) * 1000
    return result


# --------------------------------------------------------------------------- #
# Fitting resources
# --------------------------------------------------------------------------- #
def _resources(ship, items, ctx, skills, result: FittingResult) -> dict:
    # Fitting OUTPUT scales with the pilot's engineering skills (CPU Management / Power Grid
    # Management) and any hull role bonus to CPU/PG — apply them like any other ship-attribute
    # bonus so the check uses the pilot's real capacity, not the untrained hull base.
    cpu_trace = AttributeTrace("cpu_output", ship.get(A.CPU_OUTPUT, 0.0), 0.0, "tf",
                               [Contribution("Hull base", "base", "", ship.get(A.CPU_OUTPUT, 0.0))])
    pg_trace = AttributeTrace("pg_output", ship.get(A.POWER_OUTPUT, 0.0), 0.0, "MW",
                              [Contribution("Hull base", "base", "", ship.get(A.POWER_OUTPUT, 0.0))])
    cpu_out = _ship_attr(ship, A.CPU_OUTPUT, ctx, skills, trace=cpu_trace)
    pg_out = _ship_attr(ship, A.POWER_OUTPUT, ctx, skills, trace=pg_trace)
    cal_out = _ship_attr(ship, A.CALIBRATION, ctx, skills)  # no skill affects calibration today
    cpu_trace.final, pg_trace.final = round(cpu_out, 2), round(pg_out, 2)
    result.traces["cpu_output"], result.traces["pg_output"] = cpu_trace, pg_trace

    cpu_used = pg_used = cal_used = 0.0
    slot_counts = {k: 0 for k in ("high", "med", "low", "rig", "subsystem", "drone")}
    turrets = launchers = 0

    for m, a, info in items:
        if m.slot in (SlotKind.HIGH, SlotKind.MED, SlotKind.LOW) and m.state != ModuleState.OFFLINE:
            # A module's CPU/PG cost is reduced by fitting skills (Weapon Upgrades cuts turret/
            # launcher CPU, Advanced Weapon Upgrades their PG); non-matching modules get ×1.0.
            cpu_factor, _c = _bonus_factor_for_item(ctx, skills, A.CPU_USAGE, info, a)
            pg_factor, _p = _bonus_factor_for_item(ctx, skills, A.POWER_USAGE, info, a)
            cpu_used += a.get(A.CPU_USAGE, 0.0) * cpu_factor
            pg_used += a.get(A.POWER_USAGE, 0.0) * pg_factor
        if m.slot == SlotKind.RIG:
            cal_used += a.get(A.CALIBRATION_COST, 0.0)
        key = m.slot.value if m.slot.value in slot_counts else None
        if key:
            slot_counts[key] += 1
        if m.slot == SlotKind.HIGH:
            kind = _weapon_kind(info)
            turrets += kind == "turret"
            launchers += kind == "launcher"

    hull = {
        "high": int(ship.get(A.HI_SLOTS, 0)), "med": int(ship.get(A.MED_SLOTS, 0)),
        "low": int(ship.get(A.LOW_SLOTS, 0)), "rig": int(ship.get(A.RIG_SLOTS, 0)),
    }
    turret_hp = int(ship.get(A.TURRET_HARDPOINTS, 0))
    launcher_hp = int(ship.get(A.LAUNCHER_HARDPOINTS, 0))

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
                params={"used": round(used, 1), "cap": round(cap, 1), "over": round(used - cap, 1)},
            ))
    for slot, used in (("high", slot_counts["high"]), ("med", slot_counts["med"]),
                       ("low", slot_counts["low"]), ("rig", slot_counts["rig"])):
        if hull[slot] and used > hull[slot]:
            result.diagnostics.append(Diagnostic(
                "too_many_modules", Severity.ERROR, f"Too many {slot}-slot modules",
                detail=f"{used} fitted, {hull[slot]} slots", contextual=False,
                params={"slot": slot, "used": used, "total": hull[slot]},
            ))
    if turret_hp and turrets > turret_hp:
        result.diagnostics.append(Diagnostic(
            "turret_hardpoints", Severity.ERROR, "Not enough turret hardpoints",
            detail=f"{turrets} turrets, {turret_hp} hardpoints", contextual=False,
            params={"have": turrets, "cap": turret_hp}))
    if launcher_hp and launchers > launcher_hp:
        result.diagnostics.append(Diagnostic(
            "launcher_hardpoints", Severity.ERROR, "Not enough launcher hardpoints",
            detail=f"{launchers} launchers, {launcher_hp} hardpoints", contextual=False,
            params={"have": launchers, "cap": launcher_hp}))

    return {
        "cpu": {"used": round(cpu_used, 2), "output": round(cpu_out, 2)},
        "powergrid": {"used": round(pg_used, 2), "output": round(pg_out, 2)},
        "calibration": {"used": round(cal_used, 2), "output": round(cal_out, 2)},
        "slots": {"used": slot_counts, "hull": hull},
        "hardpoints": {"turret": {"used": turrets, "total": turret_hp},
                       "launcher": {"used": launchers, "total": launcher_hp}},
        "drone_bandwidth": round(ship.get(A.DRONE_BANDWIDTH, 0.0), 1),
        "drone_bay": round(ship.get(A.DRONE_CAPACITY, 0.0), 1),
    }


# --------------------------------------------------------------------------- #
# Defence (EHP)
# --------------------------------------------------------------------------- #
def _layer_hp(ship, items, hp_attr, flat_attr, pct_attr, mult_attr, ctx, skills, trace_label, result):
    """Layer HP = (base + flat module adds) × structure multipliers × rig %HP, then × the
    skill bonus. A module carries its HP bonus on a SOURCE attr distinct from the layer attr:
    shield extenders on attr 72, armour plates on 1159 (flat); shield-extender rigs on 337
    (multiplicative %, NOT stacking-penalised); nanofibers on 150 (structure HP multiplier)."""
    base = ship.get(hp_attr, 0.0)
    flat = 0.0
    pct_factor = 1.0     # rig %HP — multiplicative, stacking-EXEMPT (shield extender rigs)
    mult_factor = 1.0    # structure HP multiplier (nanofibers) — multiplicative
    for m, a, _info in items:
        if m.state == ModuleState.OFFLINE or m.slot not in HULL_FITTED_SLOTS:
            continue
        if flat_attr and flat_attr in a:
            flat += a.get(flat_attr, 0.0)
        if pct_attr and pct_attr in a:
            pct_factor *= 1.0 + a[pct_attr] / 100.0
        if mult_attr and mult_attr in a:
            mult_factor *= a[mult_attr]
    subtotal = (base + flat) * mult_factor * pct_factor
    trace = AttributeTrace(trace_label, base, base, "HP",
                           [Contribution("Hull base", "base", "", base)])
    if flat:
        trace.contributions.append(Contribution("Modules", "module", f"+{flat:.0f} HP"))
    if mult_factor != 1.0:
        trace.contributions.append(Contribution("Structure multiplier", "module", f"×{mult_factor:.3f}"))
    if pct_factor != 1.0:
        trace.contributions.append(Contribution("Rigs", "rig", f"×{pct_factor:.3f}"))
    total = _ship_attr({**ship, hp_attr: subtotal}, hp_attr, ctx, skills, trace=trace)
    trace.final = total
    result.traces[trace_label] = trace
    return total


def _layer_resonance(ship, items, resonance_attrs, module_attrs, resist_bonus_attrs,
                     ctx, skills, result, label):
    """resist% per damage type. Normal hardeners stack-penalise; the Damage-Control family
    (detected by its dogma effect) is stacking-EXEMPT. Modules may store a layer's resonance
    on a DIFFERENT attr than the ship (a Damage Control keeps HULL resonance on 974-977, not
    the ship's 109-113) — ``module_attrs`` maps each damage type to the module source attr. An
    OVERHEATED Assault DCU applies its uniform ``resistanceMultiplier`` (attr 2746) in place of
    its passive per-layer resonance."""
    def is_dcu(info):
        effs = info.get("effects", ())
        return A.EFFECT_DAMAGE_CONTROL in effs or A.EFFECT_ASSAULT_DCU in effs

    # A single overheated damage-control replaces its per-layer resonance with a flat multiplier.
    dcu_overheat = None
    for m, a, info in items:
        if m.slot not in HULL_FITTED_SLOTS:
            continue
        if m.state == ModuleState.OVERHEATED and is_dcu(info) and A.RESISTANCE_MULTIPLIER in a:
            dcu_overheat = a[A.RESISTANCE_MULTIPLIER]

    out = {}
    for dtype, attr in resonance_attrs.items():
        base = ship.get(attr, 1.0)
        mod_attr = module_attrs[dtype]
        rb_attr = resist_bonus_attrs.get(dtype) if resist_bonus_attrs else None
        pen_mults, dcu_mults = [], []
        for m, a, info in items:
            if m.state == ModuleState.OFFLINE or m.slot not in HULL_FITTED_SLOTS:
                continue
            val = a.get(mod_attr)
            if is_dcu(info):
                if dcu_overheat is None and val is not None and val != 1.0:
                    dcu_mults.append(val)          # active DCU: per-layer, stacking-exempt
            else:
                if val is not None and val != 1.0:
                    pen_mults.append(val)          # hardener with a resonance value attr
                # Active hardeners carry the resist as a % bonus on a dedicated attr instead.
                if rb_attr is not None:
                    rb = a.get(rb_attr)
                    if rb:
                        pen_mults.append(1.0 + rb / 100.0)
        resonance = base * combine_penalized(pen_mults)
        for dm in dcu_mults:
            resonance *= dm
        if dcu_overheat is not None:
            resonance *= dcu_overheat              # overheated Assault DCU: uniform multiplier
        # Unpenalised ship/skill resist bonuses on this resonance attribute.
        for spec in ctx.all():
            if spec.target_domain == "ship" and spec.target_attr == attr and not spec.penalised:
                level = skills.level(spec.skill_id) if spec.skill_id else 1
                resonance *= spec.factor(level)
        resonance = max(0.0, min(1.0, resonance))
        out[dtype] = {"resonance": resonance, "resist": 1.0 - resonance}
    return out


def _defence(ship, items, ctx, skills, op_profile, result: FittingResult) -> dict:
    layers = {}
    dp = op_profile.damage_profile.normalised().as_map()
    total_ehp = 0.0
    for name, hp_attr, res_attrs, mod_res, resist_bonus, flat_attr, pct_attr, mult_attr in (
        ("shield", A.SHIELD_HP, A.SHIELD_RESONANCE, A.SHIELD_RESONANCE, A.SHIELD_RESIST_BONUS,
         A.SHIELD_EXTENDER_HP_BONUS, A.SHIELD_RIG_HP_BONUS, None),
        ("armor", A.ARMOR_HP, A.ARMOR_RESONANCE, A.ARMOR_RESONANCE, None,
         A.ARMOR_PLATE_HP_BONUS, None, None),
        ("hull", A.HULL_HP, A.HULL_RESONANCE, A.HULL_RESONANCE_MODULE, None,
         None, None, A.STRUCTURE_HP_MULTIPLIER),
    ):
        hp = _layer_hp(ship, items, hp_attr, flat_attr, pct_attr, mult_attr,
                       ctx, skills, f"{name}_hp", result)
        res = _layer_resonance(ship, items, res_attrs, mod_res, resist_bonus,
                               ctx, skills, result, name)
        weighted_res = sum(dp[d] * res[d]["resonance"] for d in A.DAMAGE_TYPES)
        ehp = hp / weighted_res if weighted_res > 0 else hp
        total_ehp += ehp
        layers[name] = {
            "hp": round(hp, 1),
            "resists": {d: round(res[d]["resist"] * 100, 1) for d in A.DAMAGE_TYPES},
            "ehp": round(ehp, 1),
        }
    return {"layers": layers, "ehp_total": round(total_ehp, 1),
            "damage_profile": {d: round(dp[d] * 100, 1) for d in A.DAMAGE_TYPES}}


# --------------------------------------------------------------------------- #
# Capacitor
# --------------------------------------------------------------------------- #
def _capacitor(ship, items, ctx, skills, active) -> dict:
    capacity = _ship_attr(ship, A.CAP_CAPACITY, ctx, skills)
    tau_ms = _ship_attr(ship, A.CAP_RECHARGE_RATE, ctx, skills, default=0.0)
    tau = tau_ms / 1000.0
    peak = 0.5 * capacity / tau if tau > 0 else 0.0  # GJ/s, peak at 25% capacitor

    drain = 0.0
    for m, a, _info in items:
        if not active(m):
            continue
        need = a.get(A.CAP_NEED, 0.0)
        cycle_ms = a.get(A.CYCLE_TIME, 0.0) or a.get(A.RATE_OF_FIRE, 0.0)
        if need and cycle_ms > 0:
            drain += need / (cycle_ms / 1000.0)

    stable = drain <= peak and peak > 0
    stable_pct = None
    runtime_s = None
    if peak > 0:
        k = drain * tau / (2.0 * capacity) if capacity > 0 else 1.0
        disc = 1.0 - 4.0 * k
        if disc >= 0:
            u = (1.0 + math.sqrt(disc)) / 2.0
            stable_pct = round((u * u) * 100.0, 1)
        else:
            stable = False
            net = drain - peak
            runtime_s = round(capacity / net, 0) if net > 0 else None
    return {
        "capacity": round(capacity, 1),
        "recharge_s": round(tau, 1),
        "peak_recharge": round(peak, 2),
        "usage": round(drain, 2),
        "stable": stable,
        "stable_pct": stable_pct,
        "runtime_s": runtime_s,
    }


# --------------------------------------------------------------------------- #
# Offence (turret + drone DPS)
# --------------------------------------------------------------------------- #
def _weapon_damage_mult(module_attrs, info, items, ctx, skills, mod_groups):
    """Effective damage multiplier: the weapon's own multiplier (1.0 for launchers, which
    take all their damage from the charge) × unpenalised ship/role/skill bonuses × the
    stacking-penalised damage mods of the RIGHT class (``mod_groups``)."""
    base = module_attrs.get(A.DAMAGE_MULTIPLIER, 1.0)
    unpen, contribs = _bonus_factor_for_item(ctx, skills, A.DAMAGE_MULTIPLIER, info, module_attrs)
    mod_mults = []
    for m2, a2, info2 in items:
        if m2.state == ModuleState.OFFLINE:
            continue
        # A turret damage mod carries attr 64; a Ballistic Control System carries its MISSILE
        # damage bonus on attr 213 instead (launchers/BCS have no attr 64).
        if info2.get("group_id") in mod_groups:
            mv = a2.get(A.DAMAGE_MULTIPLIER) or a2.get(A.MISSILE_DAMAGE_MULT_BONUS)
            if mv:
                mod_mults.append(mv)
    return base * unpen * combine_penalized(mod_mults), contribs, mod_mults


def _weapon_rof(module_attrs, info, items, ctx, skills, mod_groups):
    base_ms = module_attrs.get(A.RATE_OF_FIRE, 0.0)
    unpen, _ = _bonus_factor_for_item(ctx, skills, A.RATE_OF_FIRE, info, module_attrs)
    rof_mults = []
    for m2, a2, info2 in items:
        if m2.state == ModuleState.OFFLINE:
            continue
        # A damage mod carries its RoF bonus on speedMultiplier (attr 204, <1 = faster) — NOT
        # on the weapon's rate-of-fire attr (51), which the mod does not have.
        if info2.get("group_id") in mod_groups and A.ROF_MULTIPLIER in a2:
            rof_mults.append(a2[A.ROF_MULTIPLIER])
    return (base_ms / 1000.0) * unpen * combine_penalized(rof_mults)


def _offence(items, provider, ctx, skills, op_profile, result: FittingResult, active) -> dict:
    turret_dps = missile_dps = missile_dps_applied = drone_dps = total_volley = 0.0
    damage_by_type = {d: 0.0 for d in A.DAMAGE_TYPES}
    weapons = has_turret = 0
    target = op_profile.target
    for m, a, info in items:
        if m.slot != SlotKind.HIGH:
            continue
        kind = _weapon_kind(info)
        is_turret = kind == "turret"
        is_launcher = kind == "launcher"
        if not (is_turret or is_launcher):
            continue
        weapons += 1
        if m.charge_type_id is None:
            result.diagnostics.append(Diagnostic(
                "missing_ammo", Severity.WARNING, "Weapon has no charge loaded",
                detail=f"type {m.type_id}", suggested_action="Load a compatible charge.",
                contextual=False, params={"type_id": m.type_id}))
            continue
        charge = provider.attrs(m.charge_type_id)
        # A hull's per-type ammo-damage bonus (e.g. "+5% kinetic missile damage / level")
        # modifies the *charge's* damage attribute, scoped to charges requiring a given
        # skill — so apply matching charge-domain bonuses to each damage type here.
        charge_info = dict(provider.type_info(m.charge_type_id) or {})
        charge_info["effects"] = frozenset(provider.effects(m.charge_type_id))
        charge_info["req_skills"] = frozenset(
            sid for sid, _ in provider.required_skills(m.charge_type_id))
        shot = {}
        for d in A.DAMAGE_TYPES:
            base = charge.get(A.CHARGE_DAMAGE[d], 0.0)
            if base:
                cf, _ = _bonus_factor_for_item(
                    ctx, skills, A.CHARGE_DAMAGE[d], charge_info, charge, domain="charge")
                base *= cf
            shot[d] = base
        shot_total = sum(shot.values())
        if shot_total <= 0:
            continue
        # Only the matching damage-mod class boosts this weapon (BCS→missiles, gyro→turrets).
        mod_groups = LAUNCHER_DAMAGE_MOD_GROUPS if is_launcher else TURRET_DAMAGE_MOD_GROUPS
        dmg_mult, _c, _g = _weapon_damage_mult(a, info, items, ctx, skills, mod_groups)
        rof_s = _weapon_rof(a, info, items, ctx, skills, mod_groups)
        if rof_s <= 0:
            continue
        volley = shot_total * dmg_mult
        dps = volley / rof_s
        if is_launcher:
            missile_dps += dps
            applied = dps
            if target is not None:
                applied = dps * missile_application(
                    target.signature_radius, target.velocity,
                    charge.get(A.AOE_CLOUD_SIZE, 0.0), charge.get(A.AOE_VELOCITY, 0.0),
                    charge.get(A.AOE_DAMAGE_REDUCTION_FACTOR, 0.0),
                    charge.get(A.AOE_DAMAGE_REDUCTION_SENSITIVITY, 0.0))
            missile_dps_applied += applied
        else:
            turret_dps += dps
            has_turret = 1
        total_volley += volley
        for d in A.DAMAGE_TYPES:
            damage_by_type[d] += (shot[d] * dmg_mult) / rof_s

    # Drones — damage scales with the drone-damage skills (Drone Interfacing + racial/size),
    # applied through the SAME unpenalised bonus engine as turrets (matched to category 18);
    # the drone loop previously read the raw multiplier and applied no skills at all.
    for m, a, info in items:
        if m.slot != SlotKind.DRONE or not active(m):
            continue
        shot = {d: a.get(A.CHARGE_DAMAGE[d], 0.0) for d in A.DAMAGE_TYPES}
        shot_total = sum(shot.values())
        base_mult = a.get(A.DRONE_DAMAGE_MULTIPLIER, 1.0)
        skill_factor, _c = _bonus_factor_for_item(ctx, skills, A.DRONE_DAMAGE_MULTIPLIER, info, a)
        # Drone Damage Amplifiers (group 640) stack-penalise among themselves.
        amp_mults = [a2[A.DRONE_DAMAGE_MULTIPLIER] for m2, a2, info2 in items
                     if info2.get("group_id") in DRONE_DAMAGE_MOD_GROUPS
                     and m2.state != ModuleState.OFFLINE and A.DRONE_DAMAGE_MULTIPLIER in a2]
        mult = base_mult * skill_factor * combine_penalized(amp_mults)
        rof_s = (a.get(A.RATE_OF_FIRE, 0.0) or 0.0) / 1000.0
        if shot_total > 0 and rof_s > 0:
            d_dps = (shot_total * mult) / rof_s * m.quantity
            drone_dps += d_dps
            for d in A.DAMAGE_TYPES:
                damage_by_type[d] += (shot[d] * mult) / rof_s * m.quantity

    total = turret_dps + missile_dps + drone_dps
    dist = {d: round(damage_by_type[d] / total * 100, 1) for d in A.DAMAGE_TYPES} if total > 0 else \
        {d: 0.0 for d in A.DAMAGE_TYPES}
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
    }
    if target is not None:
        # Missiles get true application vs the target profile; turrets/drones are reported
        # at full output (turret tracking is not modelled yet — flagged, never faked).
        out["missile_dps_applied"] = round(missile_dps_applied, 1)
        out["missile_application"] = (round(missile_dps_applied / missile_dps, 3)
                                      if missile_dps > 0 else None)
        out["applied_total_dps"] = round(turret_dps + missile_dps_applied + drone_dps, 1)
        out["target"] = {"signature_radius": target.signature_radius,
                         "velocity": target.velocity, "label": target.label}
        if has_turret:
            result.unsupported.append("turret_application_not_modelled")
    return out


# --------------------------------------------------------------------------- #
# Mobility
# --------------------------------------------------------------------------- #
def _mobility(ship, items, ctx, skills, op_profile, result, active) -> dict:
    trace = AttributeTrace("max_velocity", ship.get(A.MAX_VELOCITY, 0.0), 0.0, "m/s",
                           [Contribution("Hull base", "base", "", ship.get(A.MAX_VELOCITY, 0.0))])
    base_v = _ship_attr(ship, A.MAX_VELOCITY, ctx, skills, trace=trace)
    # Nanofibers/inertia mods carry velocity & agility MULTIPLIERS (stacking-penalised).
    vel_mults = [1.0 + a.get(A.VELOCITY_BONUS_MOD, 0.0) / 100.0
                 for m, a, _i in items if m.state != ModuleState.OFFLINE and A.VELOCITY_BONUS_MOD in a]
    base_v *= combine_penalized(vel_mults)

    agility = _ship_attr(ship, A.AGILITY, ctx, skills)  # Evasive Maneuvering + Spaceship Command
    agi_mults = [1.0 + a.get(A.AGILITY_MULTIPLIER, 0.0) / 100.0
                 for m, a, _i in items if m.state != ModuleState.OFFLINE and A.AGILITY_MULTIPLIER in a]
    agility *= combine_penalized(agi_mults)

    mass = ship.get(A.MASS, 0.0)
    sig = ship.get(A.SIGNATURE_RADIUS, 0.0)
    # Shield extenders add a FLAT signature penalty (regardless of propulsion state).
    sig += sum(a.get(A.SIG_RADIUS_ADD, 0.0) for m, a, _i in items if m.state != ModuleState.OFFLINE)
    mwd_sig_role = ship.get(A.MWD_SIG_ROLE_BONUS, 0.0)  # hull role reducing the MWD sig penalty

    prop_v = base_v
    if op_profile.propulsion_active:
        for m, a, info in items:
            if info.get("group_id") in PROP_GROUPS and active(m):
                mass += a.get(A.MASS_ADDITION, 0.0)
                # Real mass-dependent thrust: boost = (speedFactor·accel-skill/100)·(thrust/mass).
                sf_value = a.get(A.SPEED_BONUS, 0.0)
                accel_factor, _c = _bonus_factor_for_item(ctx, skills, A.SPEED_BONUS, info, a)
                sf = sf_value * accel_factor
                thrust = a.get(A.SPEED_BOOST_FACTOR, 0.0)
                hull_mass = ship.get(A.MASS, 0.0)
                if sf and thrust and hull_mass > 0:
                    boost = (sf / 100.0) * (thrust / mass)   # exact mass-dependent thrust
                elif sf:
                    # Fallback when the hull's base mass is unavailable (SDE lacks attr 4):
                    # treat the speed factor as a flat % so velocity never explodes.
                    boost = sf / 100.0
                    result.unsupported.append("prop_velocity_approx_no_mass")
                else:
                    boost = 0.0
                if boost:
                    prop_v = base_v * (1.0 + boost)
                    trace.contributions.append(
                        Contribution(info.get("name", "Propulsion"), "module", f"+{boost * 100:.0f}% speed"))
                sig_bonus = a.get(A.SIGNATURE_RADIUS_BONUS, 0.0)
                if sig_bonus:  # MWD sig penalty, reduced by any hull role bonus (attr 1803)
                    sig = sig * (1.0 + (sig_bonus / 100.0) * (1.0 + mwd_sig_role / 100.0))
                break
    trace.final = round(prop_v, 1)
    result.traces["max_velocity"] = trace
    align = _LN_10_PCT * mass * agility / 1_000_000.0 if mass and agility else 0.0
    return {
        "max_velocity": round(base_v, 1),
        "propulsion_velocity": round(prop_v, 1),
        "align_time_s": round(align, 2),
        "mass": round(mass, 0),
        "agility": round(agility, 4),
        "signature_radius": round(sig, 1),
        "warp_speed": round(ship.get(A.WARP_SPEED_MULT, 0.0), 2),
    }


# --------------------------------------------------------------------------- #
# Targeting / utility
# --------------------------------------------------------------------------- #
def _targeting(ship, ctx, skills) -> dict:
    # Max targeting range and scan resolution scale with pilot skills (Long Range Targeting,
    # Signature Analysis); this function previously read the raw hull attrs and ignored skills.
    sensors = {k: ship.get(v, 0.0) for k, v in A.SENSOR_STRENGTHS.items()}
    strongest = max(sensors.items(), key=lambda kv: kv[1]) if sensors else ("", 0.0)
    return {
        "max_target_range": round(_ship_attr(ship, A.MAX_TARGET_RANGE, ctx, skills), 0),
        "max_locked_targets": int(ship.get(A.MAX_LOCKED_TARGETS, 0)),
        "scan_resolution": round(_ship_attr(ship, A.SCAN_RESOLUTION, ctx, skills), 0),
        "sensor_strength": round(strongest[1], 1),
        "sensor_type": strongest[0],
    }


def _utility(ship, items) -> dict:
    return {
        "cargo": round(ship.get(A.CAPACITY_CARGO, 0.0), 1),
        "drone_bay": round(ship.get(A.DRONE_CAPACITY, 0.0), 1),
    }


def _ewar(items, active) -> dict:
    """Strength + engagement range of fitted electronic-warfare modules, grouped by their
    CCP inventory group. Reports each module's own strength attribute and range honestly;
    where the engine does not yet apply a scaling skill/bonus, the base module value stands
    (never inflated). Modules that are offline are excluded."""
    entries: list[dict] = []
    for m, a, info in items:
        gid = info.get("group_id")
        if gid not in EWAR_GROUPS or not active(m):
            continue
        e = {"type_id": m.type_id, "name": info.get("name", f"Type {m.type_id}"),
             "group_id": gid, "optimal_m": round(a.get(A.OPTIMAL_RANGE, 0.0), 0),
             "falloff_m": round(a.get(A.FALLOFF, 0.0), 0)}
        if gid == EWAR_WARP_SCRAMBLER:
            e.update(kind="warp_disruption",
                     strength=round(a.get(A.WARP_SCRAMBLE_STRENGTH, 0.0), 1), unit="points")
        elif gid == EWAR_STASIS_WEB:
            e.update(kind="stasis_web",
                     strength=round(abs(a.get(A.SPEED_BONUS, 0.0)), 1), unit="% speed")
        elif gid in (EWAR_ENERGY_NEUT, EWAR_ENERGY_NOS):
            is_neut = gid == EWAR_ENERGY_NEUT
            amt = a.get(A.ENERGY_NEUTRALISER_AMOUNT if is_neut else A.POWER_TRANSFER_AMOUNT, 0.0)
            cyc = (a.get(A.CYCLE_TIME, 0.0) or 0.0) / 1000.0
            e.update(kind="energy_neutraliser" if is_neut else "nosferatu",
                     strength=round(amt, 1), unit="GJ/cycle",
                     per_second=round(amt / cyc, 1) if cyc > 0 else 0.0)
        elif gid == EWAR_TARGET_PAINTER:
            e.update(kind="target_painter",
                     strength=round(a.get(A.SIGNATURE_RADIUS_BONUS_ATTR, 0.0), 1), unit="% sig")
        elif gid == EWAR_SENSOR_DAMP:
            e.update(kind="sensor_dampener", unit="%",
                     lock_range_bonus=round(a.get(A.MAX_TARGET_RANGE_BONUS, 0.0), 1),
                     scan_res_bonus=round(a.get(A.SCAN_RESOLUTION_BONUS, 0.0), 1))
        elif gid == EWAR_ECM:
            strengths = {k: a.get(v, 0.0) for k, v in A.ECM_STRENGTH.items()}
            best = max(strengths.items(), key=lambda kv: kv[1]) if strengths else ("", 0.0)
            e.update(kind="ecm", strength=round(best[1], 1), unit="points", jam_type=best[0],
                     jam_strengths={k: round(v, 1) for k, v in strengths.items()})
        elif gid == EWAR_WEAPON_DISRUPTOR:
            e.update(kind="weapon_disruptor", strength=0.0, unit="")
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


def _finalise_status(result: FittingResult, resources: dict) -> None:
    codes = {d.code for d in result.diagnostics if d.severity == Severity.ERROR}
    structural = {"too_many_modules", "turret_hardpoints", "launcher_hardpoints"}
    resource = {"cpu_exceeded", "powergrid_exceeded", "calibration_exceeded"}
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
