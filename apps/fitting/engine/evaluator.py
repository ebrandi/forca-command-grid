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
from dataclasses import replace
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
    TargetProfile,
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
ATTR_DURATION = 73

# --- WS-6 projected effects (incoming neut/nos pressure + remote reps) --------------
# Verified live 2026-07-21. neut/nos amounts and remote-rep amounts are self-contained on
# the projecting module (no dogma modifier), so the evaluator reads them directly and
# scales by the hull's evaluated resistance. Detection is by the module's DEFAULT effect id
# (unambiguous across metalevels): energyNeutralizerFalloff 6187 / energyNosferatuFalloff
# 6197 / shipModuleRemoteShieldBooster 6186 / shipModuleRemoteArmorRepairer 6188 /
# shipModuleRemoteHullRepairer 6185.
EFFECT_PROJ_WEB = 6426          # remoteWebifierFalloff    (stat via graph: maxVelocity)
EFFECT_PROJ_PAINT = 6425        # remoteTargetPaintFalloff (stat via graph: signatureRadius)
EFFECT_PROJ_DAMP = 6422         # remoteSensorDampFalloff  (stat via graph: range/scanRes)
EFFECT_PROJ_SCRAM = 5934        # warpScrambleBlockMWDWithNPCEffect (real graph)
EFFECT_PROJ_NEUT = 6187
EFFECT_PROJ_NOS = 6197
EFFECT_PROJ_RSB = 6186          # remote shield booster  → shieldBonus 68
EFFECT_PROJ_RAR = 6188          # remote armor repairer  → armorDamageAmount 84
EFFECT_PROJ_RHR = 6185          # remote hull repairer   → structureDamageAmount 83
ATTR_SPEED_FACTOR = 20          # web: % max-velocity bonus (negative)
ATTR_SIG_RADIUS_BONUS = 554     # painter: % signature bonus
ATTR_MAX_TGT_RANGE_BONUS = 309  # damp: % lock-range bonus (negative)
ATTR_SCAN_RES_BONUS = 566       # damp: % scan-resolution bonus (negative) — 566 not 565
ATTR_WARP_SCRAMBLE_STRENGTH = 105
ATTR_ENERGY_NEUT_AMOUNT = 97
ATTR_POWER_TRANSFER_AMOUNT = 90
ATTR_STRUCTURE_DAMAGE_AMOUNT = 83   # remote HULL repair per cycle (87 is shieldTransferRange)
ATTR_ENERGY_WARFARE_RESIST = 2045   # hull energyWarfareResistance (neut/nos; default 1.0)
ATTR_REMOTE_REP_IMPEDANCE = 2116    # hull remoteRepairImpedance (remote reps; default 1.0)
# The projected remote-rep layers: (telemetry layer, default effect id, per-cycle attr).
_PROJ_REP_LAYERS = (("shield", EFFECT_PROJ_RSB, ATTR_SHIELD_BOOST),
                    ("armor", EFFECT_PROJ_RAR, ATTR_ARMOR_REPAIR),
                    ("hull", EFFECT_PROJ_RHR, ATTR_STRUCTURE_DAMAGE_AMOUNT))
_RECHARGE_PEAK = 2.5            # peak dC/dt = 2.5·Cmax/τ, at 25% charge
AU_METERS = 149_597_870_700    # one astronomical unit in metres (CCP's warp-maths unit)
_DRONE_MOBILE_MIN_SPEED = 1.0  # maxVelocity above this = a chasing (mobile) drone, not a
#                                sentry (pyfa's `droneSpeed > 1` sentry/mobile split)

# --- WS-8 mining yield telemetry -----------------------------------------------------
# Verified live 2026-07-21 (scout-data §E + engine end-to-end). A mining module carries
# miningAmount(77) = the volume harvested per cycle and duration(73) = the cycle time; both
# are evaluated through the graph, so hull role/skill bonuses and — for a modulated strip
# miner — the loaded crystal's yield multiplier already flow into miningAmount. The crystal
# mechanic is NOT a 789→77 preAssign (the scout's early read): a mining crystal (charge) is
# an ItemModifier preMul on the module's miningAmount(77) by the crystal's
# specializationAsteroidYieldMultiplier(782), so the evaluated 77 is already the crystal-
# boosted yield (Modulated Strip Miner II 120 × Veldspar T1 1.625 = 195, no skills).
ATTR_MINING_AMOUNT = 77
ATTR_MINING_WASTE_PROBABILITY = 3154   # % chance the module wastes (loses) ore that cycle
ATTR_MINING_WASTE_VOLUME_MULT = 3153   # multiplier on the wasted volume when waste occurs
EFFECT_MINING_LASER = 67               # miningLaser (ore + ice modules; category 2)
EFFECT_MINING_CLOUDS = 2726            # miningClouds (gas cloud harvesters; category 2)
EFFECT_MINING_DRONE = 17               # mining (mining/ice-harvesting drones; category 2)
_MINING_MODULE_EFFECTS = frozenset({EFFECT_MINING_LASER, EFFECT_MINING_CLOUDS})
SKILL_ICE_HARVESTING = 16281           # a module requiring this skill is an ice harvester

# --- WS-9 exotic weapons (smartbombs, vorton projectors, breacher pods) ---------------
# Verified live 2026-07-21 (scout-data §F + type dumps).
# Smartbombs: every published Smart Bomb (group 72) fires the same default effect empWave
# (38, category 1 active); damage lives on the MODULE (em/th/kin/exp 114/116/117/118, one
# type populated), range on empFieldRange(99), cycle on duration(73); no charge → no reload.
EFFECT_SMARTBOMB = 38
ATTR_EMP_FIELD_RANGE = 99
# Vorton projectors (group 4060) fire Condenser Pack charges. The module carries
# damageMultiplier(64) + RoF speed(51); the charge carries the em/kin/… damage and the
# missile-style AoE application attrs (aoeVelocity 653 / aoeCloudSize 654 / aoeDRF 1353). The
# arc that chains to VortonArcTargets(3037) secondaries within VortonArcRange(3036) is NOT
# modelled in v1 (primary-target damage only — documented in the matrix). Default firing
# effect ChainLightning(8037, category 2). turretFitted(42) marks the hardpoint.
VORTON_GROUP = 4060
EFFECT_VORTON = 8037
ATTR_VORTON_ARC_RANGE = 3036
ATTR_VORTON_ARC_TARGETS = 3037
# Breacher pod launchers (group 4807) share effect 101 (useMissiles) with missile launchers,
# so they MUST be intercepted before the missile path. The SCARAB Breacher Pod charge carries
# no direct damage — it applies a non-stacking damage-over-time: dotMaxDamagePerTick(5736)
# flat OR dotMaxHPPercentagePerTick(5737) percent-of-total-HP per 1-second tick (whichever is
# lower), for dotDuration(5735) ms. The DoT does not stack across launchers (strongest wins).
GROUP_BREACHER_LAUNCHER = 4807
ATTR_DOT_DURATION = 5735
ATTR_DOT_MAX_DAMAGE_PER_TICK = 5736
ATTR_DOT_MAX_HP_PCT_PER_TICK = 5737
_DOT_TICK_S = 1.0                       # CCP breacher pods tick once per second

# --- WS-10 EWAR application ------------------------------------------------------------
# Each OUR offensive-ewar module is classified by its DEFAULT (identifying) effect id, not
# its inventory group. The group-id approach the old readout used had already mislabelled
# two families (painter as 209, really group 379; weapon disruptor as 213, really 291) —
# effect ids are unambiguous across metalevels AND avoid the attribute collision that made
# group detection fragile (a painter's signatureRadiusBonus 554 is the same attribute an MWD
# carries, so attribute-presence alone can't tell them apart, but the identifying effect can).
# All ids verified against the live DB 2026-07-21 (default effect + effectCategory dumps).
EFFECT_EWAR_ECM = 6470          # remoteECMFalloff        (targeted jammer, cat 2)
EFFECT_EWAR_ECM_BURST = 6714    # ECMBurstJammer          (AoE burst jammer, cat 1)
EFFECT_EWAR_DAMP = 6422         # remoteSensorDampFalloff (cat 2)
EFFECT_EWAR_PAINT = 6425        # remoteTargetPaintFalloff (cat 2)
EFFECT_EWAR_WEB = 6426          # remoteWebifierFalloff   (cat 2)
EFFECT_EWAR_SCRAM = 5934        # warpScrambleBlockMWDWithNPCEffect (scrambler, cat 2)
EFFECT_EWAR_DISRUPT = 39        # warpDisrupt             (warp disruptor / point, cat 2)
EFFECT_EWAR_TD = 6424           # shipModuleTrackingDisruptor  (cat 2)
EFFECT_EWAR_GD = 6423           # shipModuleGuidanceDisruptor  (cat 2)
# effect id → ewar kind. neut/nos reuse their WS-6 default effect ids (EFFECT_PROJ_NEUT/NOS);
# they are cross-linked into the ewar section AND drive the capacitor drain in _capacitor.
_EWAR_EFFECT_KIND = {
    EFFECT_EWAR_ECM: "ecm",
    EFFECT_EWAR_ECM_BURST: "ecm_burst",
    EFFECT_EWAR_DAMP: "sensor_dampener",
    EFFECT_EWAR_PAINT: "target_painter",
    EFFECT_EWAR_WEB: "stasis_web",
    EFFECT_EWAR_SCRAM: "warp_disruption",
    EFFECT_EWAR_DISRUPT: "warp_disruption",
    EFFECT_EWAR_TD: "tracking_disruptor",
    EFFECT_EWAR_GD: "guidance_disruptor",
    EFFECT_PROJ_NEUT: "energy_neutraliser",
    EFFECT_PROJ_NOS: "nosferatu",
}
_SENSOR_TYPES = ("radar", "ladar", "magnetometric", "gravimetric")
# The stacking-penalty factor, reproduced from graph._PENALTY_FACTOR so the ewar-adjusted
# target profile runs the SAME maths a fitted/projected postPercent modifier does
# (current *= 1 + v·factor^(i²), penalising by |v| descending). Kept inline like _LN_ALIGN.
_EWAR_PENALTY = math.exp(-((1.0 / 2.67) ** 2))

# --- WS-12 fighters (carrier / supercarrier squadrons) --------------------------------
# Verified live 2026-07-21 (scout-data §G + full type/effect/modifier dumps). A fighter
# squadron rides the fit as a graph entity (graph.build_entities) and is one of the ship's
# "located" items, so the carrier's fighter-damage hull trait and the fighter skills
# (Fighters, racial Fighter Specialization, Heavy Fighters, Drone Interfacing) apply to its
# damage multipliers through the ordinary OwnerRequiredSkillModifier pipeline — all 82 such
# modifier rows filter on a fighter's required skill (e.g. Fighters 23069), so NO fighter-
# specific modifier code is needed here. Only the DPS read-out and structural validation live
# in pass 4. Ship-side slot attrs default 0.0, so a non-carrier reads 0 tubes/slots/bay.
ATTR_FIGHTER_TUBES = 2216
ATTR_FIGHTER_LIGHT_SLOTS = 2217
ATTR_FIGHTER_SUPPORT_SLOTS = 2218
ATTR_FIGHTER_HEAVY_SLOTS = 2219
ATTR_FIGHTER_CAPACITY = 2055           # fighter bay volume (m3)
ATTR_FIGHTER_SQUADRON_MAX_SIZE = 2215  # on the FIGHTER type; the per-squadron count cap
# Standard-attack ability families. Every shipped fighter's default attack is the missile-
# style AttackM (fighterAbilityAttackMissile*, effect 6465): duration 2233, damage 2227-2230,
# multiplier 2226 (the attr the skill/hull damage bonuses postPercent). The turret family
# (2171-2178) exists in the dogma schema but no shipped fighter carries it (0 types have 2171)
# — kept for robustness/forward-compat. The fighter's SPECIAL long-range volley
# (fighterAbilityMissiles*, effect 6431, non-default) and its utility abilities (web/neut/MWD)
# are NOT counted in v1 (documented gap: they are separately-toggled abilities, off by default
# in pyfa too — eos/saveddata/fighter.py activates only fighterAbilityAttackM).
ATTR_FA_ATK_MISSILE_MULT = 2226
ATTR_FA_ATK_MISSILE_DMG = {"em": 2227, "thermal": 2228, "kinetic": 2229, "explosive": 2230}
ATTR_FA_ATK_MISSILE_DURATION = 2233
ATTR_FA_ATK_TURRET_MULT = 2178
ATTR_FA_ATK_TURRET_DMG = {"em": 2171, "thermal": 2172, "kinetic": 2173, "explosive": 2174}
ATTR_FA_ATK_TURRET_DURATION = 2177
CATEGORY_FIGHTER = 87
# Fighter GROUP → (role token, the hull attribute counting that role's tubes). Structure
# fighter groups (4777/4778/4779) map to no standard-ship slot (only Upwell structures carry
# them), so a structure fighter on a ship gets 0 role slots → rejected.
_FIGHTER_GROUP_ROLE = {
    1652: ("light", ATTR_FIGHTER_LIGHT_SLOTS),
    1537: ("support", ATTR_FIGHTER_SUPPORT_SLOTS),
    1653: ("heavy", ATTR_FIGHTER_HEAVY_SLOTS),
    4777: ("structure_light", None),
    4778: ("structure_support", None),
    4779: ("structure_heavy", None),
}

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
    # WS-10: our active painters/webs/damps adjust the target profile the offence numbers
    # are applied against (painter enlarges its signature, web slows it) — computed once and
    # threaded into _offence (which keeps the raw-profile totals as *_unassisted) and into
    # the ewar section (as ewar_on_target).
    ewar_adjusted, ewar_on_target = _ewar_target_adjustment(ev, op_profile)
    # WS-12: fighter squadrons are computed first so their DPS folds into the offence totals
    # (raw output only — applied DPS is not modelled for fighters in v1). The section itself is
    # exposed separately below.
    fighters_section, fighter_agg = _fighters(ev, provider, result)
    offence = _offence(ev, provider, op_profile, result, ewar_adjusted, fighter_agg)
    mobility = _mobility(ev, op_profile, result)
    targeting = _targeting(ev, op_profile)
    utility = {"cargo": round(ev.ship_value(ATTR_CAPACITY), 1),
               "drone_bay": round(ev.ship_value(A.DRONE_CAPACITY), 1)}
    ewar = _ewar(ev, op_profile, ewar_on_target, provider)
    industry = _industry(ev, provider)
    projected = _projected(ev, provider)
    boosts = _boosts(ev, provider, result)
    _validate_restrictions(ev, provider, result)
    _validate_mode(ev, provider, result)
    _validate_projected(ev, provider, result)
    _validate_mutated(fit, provider, result)

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
        "utility": utility, "ewar": ewar, "projected": projected, "boosts": boosts,
        "fighters": fighters_section, "ship": ship_section,
        "operating_profile": {
            "propulsion_active": op_profile.propulsion_active,
            "damage_profile": op_profile.damage_profile.normalised().as_map(),
        },
    }
    # Mining telemetry is present only when the fit actually mines — no empty-section noise.
    if industry is not None:
        result.telemetry["industry"] = industry
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
                            ("hull", ATTR_STRUCTURE_DAMAGE_AMOUNT)):
            amount = ev.value(m, attr) if attr in m.base else 0.0
            if amount:
                reps[layer] += amount / (cycle / 1000.0)

    # WS-6: friendly remote reps projected ONTO us add incoming HP/s per layer, reported
    # SEPARATELY from our own active tank (never folded in silently). Each remote booster's
    # per-cycle amount (shieldBonus 68 / armorDamageAmount 84 / structureDamageAmount 83) is
    # read directly off the projecting module and scaled by our remoteRepairImpedance (2116;
    # default 1.0, driven far below 1 by a siege module for the "cannot be remote-repped"
    # rule). This is incoming assistance, so it is not resistance-reduced like damage.
    rep_impedance = max(0.0, ev.ship_value(ATTR_REMOTE_REP_IMPEDANCE))
    incoming_rep = {"shield": 0.0, "armor": 0.0, "hull": 0.0}
    for p in ev.projected:
        for layer, eff, attr in _PROJ_REP_LAYERS:
            if eff not in p.effect_ids:
                continue
            cyc = ev.value(p, ATTR_DURATION)
            amt = ev.value(p, attr)
            if cyc > 0 and amt:
                incoming_rep[layer] += (amt / (cyc / 1000.0)) * rep_impedance

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
        "incoming_rep": {
            "shield_hps": round(incoming_rep["shield"], 1),
            "armor_hps": round(incoming_rep["armor"], 1),
            "hull_hps": round(incoming_rep["hull"], 1),
            "total_hps": round(sum(incoming_rep.values()), 1),
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

    # WS-6: projected energy neutralizers / nosferatus drain us. Both are read directly off
    # the projecting module (energyNeutralizerAmount 97 / powerTransferAmount 90 per cycle)
    # and scaled by our evaluated energyWarfareResistance (2045; a fitted cap battery lowers
    # it via effect 6487). NOS is a v1 simplification: modelled as pure incoming drain (its
    # real "only while attacker cap < target cap" peak-balance rule is noted in the matrix).
    ew_resist = max(0.0, ev.ship_value(ATTR_ENERGY_WARFARE_RESIST))
    incoming_pressure = 0.0
    for p in ev.projected:
        if EFFECT_PROJ_NEUT in p.effect_ids:
            amt_attr = ATTR_ENERGY_NEUT_AMOUNT
        elif EFFECT_PROJ_NOS in p.effect_ids:
            amt_attr = ATTR_POWER_TRANSFER_AMOUNT
        else:
            continue
        cyc = ev.value(p, ATTR_DURATION)
        amt = ev.value(p, amt_attr)
        if cyc > 0 and amt:
            incoming_pressure += (amt / (cyc / 1000.0)) * ew_resist

    net_drain = drain - injection + incoming_pressure
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
        "incoming_pressure": round(incoming_pressure, 2),
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


def _offence(ev: EvaluatedFit, provider, op_profile: OperatingProfile, result,
             applied_target: TargetProfile | None = None,
             fighter_agg: dict | None = None) -> dict:
    turret_dps = missile_dps = missile_dps_applied = drone_dps = total_volley = 0.0
    turret_dps_applied = drone_dps_applied = 0.0
    # WS-10: parallel "unassisted" applied totals measured against the RAW target (no ewar
    # help), so the UI can show applied-with-ewar and applied-without side by side.
    turret_dps_applied_raw = missile_dps_applied_raw = drone_dps_applied_raw = 0.0
    sustained_weapon_dps = 0.0
    # WS-9 exotic weapons. Smartbombs + vorton projectors deal typed damage (counted into
    # damage_by_type); breacher pods deal a typeless damage-over-time that does NOT stack
    # across launchers, so their entries are collected and only the strongest one counts
    # toward the fit totals (see the cap below).
    smartbomb_dps = smartbomb_applied = smartbomb_sustained = 0.0
    vorton_dps = vorton_applied = vorton_sustained = 0.0
    breacher_entries: list[dict] = []
    damage_by_type = {d: 0.0 for d in A.DAMAGE_TYPES}
    weapons = 0
    ranges: list[dict] = []
    raw_target = op_profile.target
    # WS-10: applied DPS is measured against the ewar-ADJUSTED target when one is supplied
    # (our painters enlarge its signature, our webs slow it → its derived angular drops);
    # the raw-profile numbers are kept in parallel as the *_unassisted totals (decision
    # documented in the mechanics handbook). With no ewar adjustment the two are identical.
    target = applied_target if applied_target is not None else raw_target
    has_assist = applied_target is not None
    # Turret/drone application needs a distance (range term) and an angular speed (tracking
    # term). Missiles need only sig/velocity, so they apply whenever a target is set.
    angular = target.effective_angular() if target is not None else None
    raw_angular = raw_target.effective_angular() if raw_target is not None else None
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
        is_smartbomb = EFFECT_SMARTBOMB in m.effect_ids
        is_vorton = m.group_id == VORTON_GROUP or EFFECT_VORTON in m.effect_ids
        # A breacher pod launcher shares effect 101 with missile launchers, so it must be
        # recognised by its own group and handled before the missile path (its pods carry no
        # direct damage — the missile path would read a zero volley and drop it silently).
        is_breacher = m.group_id == GROUP_BREACHER_LAUNCHER
        if not (is_turret or is_launcher or is_smartbomb or is_vorton or is_breacher):
            continue
        weapons += 1
        if not _active(m):
            continue                          # online weapons don't fire

        # --- WS-9 exotic weapons ---------------------------------------------------
        if is_smartbomb:
            entry, sb_dps, sb_volley = _smartbomb_entry(ev, m, provider, target)
            smartbomb_dps += sb_dps
            smartbomb_applied += entry.get("applied_dps") or 0.0
            smartbomb_sustained += entry.get("sustained_dps") or sb_dps
            total_volley += sb_volley
            by_type = entry.pop("_by_type")
            for d in A.DAMAGE_TYPES:
                damage_by_type[d] += by_type[d]
            ranges.append(entry)
            continue
        if is_breacher:
            if m.charge is None:
                result.diagnostics.append(Diagnostic(
                    "missing_ammo", Severity.WARNING, "Weapon has no charge loaded",
                    detail=f"type {m.type_id}", suggested_action="Load a compatible charge.",
                    contextual=False, params={"type_id": m.type_id}))
                continue
            entry, bre = _breacher_entry(ev, m, provider, target)
            breacher_entries.append(bre)
            ranges.append(entry)
            continue
        if is_vorton:
            if m.charge is None:
                result.diagnostics.append(Diagnostic(
                    "missing_ammo", Severity.WARNING, "Weapon has no charge loaded",
                    detail=f"type {m.type_id}", suggested_action="Load a compatible charge.",
                    contextual=False, params={"type_id": m.type_id}))
                continue
            entry, vo_dps, vo_volley = _vorton_entry(ev, m, provider, target)
            if entry is None:
                continue
            vorton_dps += vo_dps
            vorton_applied += entry.get("applied_dps") or 0.0
            vorton_sustained += entry.get("sustained_dps") or vo_dps
            total_volley += vo_volley
            by_type = entry.pop("_by_type")
            for d in A.DAMAGE_TYPES:
                damage_by_type[d] += by_type[d]
            ranges.append(entry)
            continue

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
                    tracking = ev.value(m, A.TRACKING_SPEED)
                    osig = ev.value(m, A.OPTIMAL_SIG_RADIUS)
                    optimal = ev.value(m, A.OPTIMAL_RANGE)
                    falloff = ev.value(m, A.FALLOFF)
                    amult = _applied_turret_multiplier(_turret_cth(
                        tracking, osig, optimal, falloff,
                        angular, target.signature_radius, target.target_distance_m))
                    entry["applied_dps"] = round(dps * amult, 1)
                    entry["applied_multiplier"] = round(amult, 3)
                    turret_dps_applied += dps * amult
                    if has_assist:
                        turret_dps_applied_raw += dps * _applied_turret_multiplier(_turret_cth(
                            tracking, osig, optimal, falloff, raw_angular,
                            raw_target.signature_radius, raw_target.target_distance_m))
                    else:
                        turret_dps_applied_raw += dps * amult
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
            applied = applied_raw = dps
            if target is not None:
                er = ev.value(ch, A.AOE_CLOUD_SIZE)
                ev_vel = ev.value(ch, A.AOE_VELOCITY)
                drf = ev.value(ch, A.AOE_DAMAGE_REDUCTION_FACTOR)
                factor = _missile_application(
                    target.signature_radius, target.velocity, er, ev_vel, drf)
                applied = dps * factor
                entry["applied_dps"] = round(applied, 1)
                entry["applied_multiplier"] = round(factor, 3)
                applied_raw = (dps * _missile_application(
                    raw_target.signature_radius, raw_target.velocity, er, ev_vel, drf)
                    if has_assist else applied)
            missile_dps_applied += applied
            missile_dps_applied_raw += applied_raw
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
                    if has_assist:
                        d_raw = _drone_applied(ev, dr, d_dps, raw_target, raw_angular,
                                               turret_complete)
                        drone_dps_applied_raw += d_raw if d_raw is not None else d_applied
                    else:
                        drone_dps_applied_raw += d_applied

    # WS-9: breacher DoT does not stack across launchers — only the strongest instance's
    # damage lands, so the fit's breacher contribution is the max single launcher, not the
    # sum (mirrors CCP: multiple pods on one target refresh, they don't add).
    breacher_dps = max((b["dps"] for b in breacher_entries), default=0.0)
    breacher_applied = max((b["applied"] for b in breacher_entries), default=0.0)
    breacher_sustained = max((b["sustained"] for b in breacher_entries), default=0.0)

    # WS-12: fighter squadrons (computed in _fighters) fold into the fit's damage exactly like
    # a weapon — typed damage into the mix + totals, volley into the fit volley, and they
    # sustain at their DPS (fighter rearm timing needs data CCP does not ship — pyfa hardcodes
    # NUM_SHOTS/REARM mappings; v1 reports the un-rearmed figure, a documented gap).
    fighter_dps = fighter_agg["dps"] if fighter_agg else 0.0
    if fighter_agg:
        for d in A.DAMAGE_TYPES:
            damage_by_type[d] += fighter_agg["by_type"][d]
        total_volley += fighter_agg["volley"]
        sustained_weapon_dps += fighter_agg["sustained"]
        # A carrier's applied DPS cannot include its fighters in v1 (own tracking model), so a
        # target-set fit with damaging squadrons is honestly reported as applied-incomplete.
        if target is not None and fighter_agg["has_damage"]:
            applied_complete = False

    # Typed damage (turrets/missiles/drones/smartbombs/vorton/fighters) drives the damage-type
    # mix; breacher DoT is typeless (bypasses resists) so it is excluded from the distribution
    # but still counted into total_dps.
    typed_total = (turret_dps + missile_dps + drone_dps + smartbomb_dps + vorton_dps
                   + fighter_dps)
    total = typed_total + breacher_dps
    dist = ({d: round(damage_by_type[d] / typed_total * 100, 1) for d in A.DAMAGE_TYPES}
            if typed_total > 0 else {d: 0.0 for d in A.DAMAGE_TYPES})
    result.traces["dps"] = AttributeTrace(
        "dps", 0.0, round(total, 1), "dps",
        [Contribution("Turrets", "module", f"{turret_dps:.1f} dps"),
         Contribution("Missiles", "module", f"{missile_dps:.1f} dps"),
         Contribution("Drones", "module", f"{drone_dps:.1f} dps"),
         Contribution("Smartbombs", "module", f"{smartbomb_dps:.1f} dps"),
         Contribution("Vorton", "module", f"{vorton_dps:.1f} dps"),
         Contribution("Breacher", "module", f"{breacher_dps:.1f} dps"),
         Contribution("Fighters", "fighter", f"{fighter_dps:.1f} dps")])
    if weapons == 0 and drone_dps == 0 and fighter_dps == 0:
        result.unsupported.append("no_weapons_detected")
    out = {
        "turret_dps": round(turret_dps, 1), "missile_dps": round(missile_dps, 1),
        "drone_dps": round(drone_dps, 1),
        "smartbomb_dps": round(smartbomb_dps, 1), "vorton_dps": round(vorton_dps, 1),
        "breacher_dps": round(breacher_dps, 1), "fighter_dps": round(fighter_dps, 1),
        "total_dps": round(total, 1), "volley": round(total_volley, 1),
        # Drones fire continuously (no magazine), so they sustain by definition. Smartbombs
        # have no magazine (sustained == dps); vorton reloads like a turret; a breacher's DoT
        # is continuous (it outlasts its own fire interval), so it sustains at its dot dps.
        "total_sustained_dps": round(sustained_weapon_dps + drone_dps + smartbomb_sustained
                                     + vorton_sustained + breacher_sustained, 1),
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
        out["smartbomb_dps_applied"] = round(smartbomb_applied, 1)
        out["vorton_dps_applied"] = round(vorton_applied, 1)
        out["breacher_dps_applied"] = round(breacher_applied, 1)
        # Applied total sums only the classes we could compute; a weapon excluded because
        # the target profile was incomplete flips applied_complete to False (never faked).
        # Smartbombs (area, auto-hit) and breacher DoT always apply; vorton applies its AoE
        # factor. All are always computable from the target profile, so none gate the flag.
        out["total_applied_dps"] = round(
            turret_dps_applied + missile_dps_applied + drone_dps_applied
            + smartbomb_applied + vorton_applied + breacher_applied, 1)
        # WS-10: the same total measured against the RAW (un-painted, un-webbed) target. Only
        # the tracking/missile families differ; smartbomb (auto-hit) and breacher (HP-based)
        # are ewar-invariant, so they carry through unchanged. Equal to total_applied_dps when
        # the fit has no active painter/web.
        out["total_applied_dps_unassisted"] = round(
            turret_dps_applied_raw + missile_dps_applied_raw + drone_dps_applied_raw
            + smartbomb_applied + vorton_applied + breacher_applied, 1)
        out["applied_complete"] = applied_complete
        # Legacy field (pre-WS-2): only missiles applied, turrets/drones raw. Kept so older
        # saved renders never KeyError; superseded by total_applied_dps above.
        out["applied_total_dps"] = round(
            turret_dps + missile_dps_applied + drone_dps, 1)
        # Echo the RAW target the pilot entered (the ewar-adjusted values live in
        # ewar.ewar_on_target), with its raw derived angular.
        out["target"] = {"signature_radius": raw_target.signature_radius,
                         "velocity": raw_target.velocity, "label": raw_target.label,
                         "distance_m": raw_target.target_distance_m, "angular": raw_angular,
                         "hp": raw_target.target_hp}
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
# Fighters (WS-12): carrier / supercarrier squadrons
# --------------------------------------------------------------------------- #
def _fighter_unit_dps(ev: EvaluatedFit, f: Entity):
    """Standard-attack output for ONE fighter of squadron ``f``: (per-fighter volley,
    per-type volley dict, cycle seconds, ability kind). A fighter with no standard attack
    (a pure support/tackle fighter, e.g. Dromi) returns a zero volley and ``None`` kind.

    The multiplier attribute (2226 / 2178) already carries every skill + carrier-hull damage
    bonus, folded in by the graph's OwnerRequiredSkillModifier pass — so reading its evaluated
    value here is all that is needed. Mirrors pyfa's fighterAbility volley (amount ×
    damage × multiplier / duration; eos/saveddata/fighterAbility.py:117-142, studied not
    copied) but evaluates the squadron count once at the squadron level."""
    if ATTR_FA_ATK_MISSILE_MULT in f.base:
        dmg_attrs, mult_attr, dur_attr = (
            ATTR_FA_ATK_MISSILE_DMG, ATTR_FA_ATK_MISSILE_MULT, ATTR_FA_ATK_MISSILE_DURATION)
    elif ATTR_FA_ATK_TURRET_MULT in f.base:
        dmg_attrs, mult_attr, dur_attr = (
            ATTR_FA_ATK_TURRET_DMG, ATTR_FA_ATK_TURRET_MULT, ATTR_FA_ATK_TURRET_DURATION)
    else:
        return 0.0, {d: 0.0 for d in A.DAMAGE_TYPES}, 0.0, None
    mult = ev.value(f, mult_attr)
    by_type = {d: (ev.value(f, dmg_attrs[d]) if dmg_attrs[d] in f.base else 0.0) * mult
               for d in A.DAMAGE_TYPES}
    cycle_s = ev.value(f, dur_attr) / 1000.0
    return sum(by_type.values()), by_type, cycle_s, "standard_attack"


def _fighters(ev: EvaluatedFit, provider, result: FittingResult):
    """Pass-4 fighter telemetry + structural validation.

    Returns ``(section, agg)``: ``section`` is the ``fighters`` telemetry block (per-squadron
    detail + fit totals); ``agg`` feeds _offence so a squadron's DPS folds into the fit's
    total DPS / volley / damage-mix exactly like a weapon. Applied (tracking-adjusted) DPS is
    NOT modelled in v1 — a fighter carries its own explosion/tracking attrs, so every squadron
    reports ``applied_dps=None`` with a reason (documented gap in the mechanics matrix)."""
    squadrons: list[dict] = []
    fighter_dps = fighter_volley_total = bay_used = 0.0
    by_type_total = {d: 0.0 for d in A.DAMAGE_TYPES}
    is_carrier = ATTR_FIGHTER_TUBES in ev.ship.base
    tubes_cap = ev.ship_value(ATTR_FIGHTER_TUBES)
    bay_cap = ev.ship_value(ATTR_FIGHTER_CAPACITY)
    role_used: dict[str, int] = {}
    role_cap: dict[str, float] = {}
    tubes_used = 0

    for f in ev.fighters:
        info = provider.type_info(f.type_id) or {}
        name = info.get("name", "")
        count = f.quantity
        # Reject a placeholder scaffold row or a non-fighter type outright (the UI filters
        # placeholders, but the engine stays honest if one arrives). It contributes nothing.
        if "PLACEHOLDER" in name.upper() or f.category_id != CATEGORY_FIGHTER:
            result.diagnostics.append(Diagnostic(
                "fighter_invalid_type", Severity.ERROR, "Not a valid fighter",
                detail=f"type {f.type_id}", contextual=False,
                suggested_action="Choose a real fighter squadron.",
                params={"type_id": f.type_id, "name": name}))
            continue

        role, role_attr = _FIGHTER_GROUP_ROLE.get(f.group_id, (None, None))
        max_size = ev.value(f, ATTR_FIGHTER_SQUADRON_MAX_SIZE)
        volume = f.base.get(ATTR_VOLUME, 0.0)
        squad_bay = volume * count
        bay_used += squad_bay
        tubes_used += 1
        if role is not None:
            role_used[role] = role_used.get(role, 0) + 1
            role_cap[role] = ev.ship_value(role_attr) if role_attr is not None else 0.0

        unit_volley, unit_by_type, cycle_s, kind = _fighter_unit_dps(ev, f)
        unit_dps = unit_volley / cycle_s if cycle_s > 0 else 0.0
        squadron_dps = unit_dps * count
        squadron_volley = unit_volley * count
        fighter_dps += squadron_dps
        fighter_volley_total += squadron_volley
        for d in A.DAMAGE_TYPES:
            by_type_total[d] += (unit_by_type[d] * count / cycle_s) if cycle_s > 0 else 0.0

        abilities = []
        if kind is not None:
            abilities.append({
                "kind": kind, "volley": round(squadron_volley, 1),
                "dps": round(squadron_dps, 1), "cycle_s": round(cycle_s, 2),
                "damage": {d: round(unit_by_type[d] * count, 1) for d in A.DAMAGE_TYPES}})

        # Per-squadron structural check: more fighters than the (evaluated) squadron cap.
        if max_size > 0 and count > max_size:
            result.diagnostics.append(Diagnostic(
                "fighter_squadron_oversized", Severity.ERROR, "Fighter squadron too large",
                detail=f"{name}: {count} of max {int(max_size)}", contextual=False,
                suggested_action="Reduce the squadron to its maximum size.",
                params={"type_id": f.type_id, "name": name, "count": count,
                        "max": int(max_size)}))

        squadrons.append({
            "type_id": f.type_id, "name": name, "role": role, "count": count,
            "max_squadron_size": int(max_size), "unit_dps": round(unit_dps, 1),
            "squadron_dps": round(squadron_dps, 1), "bay_m3": round(squad_bay, 1),
            "abilities": abilities,
            # Applied DPS is not modelled for fighters in v1 (own tracking/explosion attrs).
            "applied_dps": None, "applied_reason": "fighter_application_not_modelled"})

    # --- fit-level structural validation ---------------------------------------
    if squadrons and not is_carrier:
        # Fighters on a hull that has no fighter tubes at all: nothing else to check.
        result.diagnostics.append(Diagnostic(
            "fighter_on_non_carrier", Severity.ERROR, "This hull cannot field fighters",
            detail=f"{len(squadrons)} squadron(s) on a hull with no fighter tubes",
            contextual=False, suggested_action="Field fighters from a carrier or supercarrier.",
            params={"squadrons": len(squadrons)}))
    elif squadrons:
        if tubes_used > tubes_cap:
            result.diagnostics.append(Diagnostic(
                "fighter_tubes_exceeded", Severity.ERROR, "Not enough fighter tubes",
                detail=f"{tubes_used} squadrons, {int(tubes_cap)} tubes", contextual=False,
                suggested_action="Launch fewer squadrons.",
                params={"used": tubes_used, "cap": int(tubes_cap)}))
        for r, used in sorted(role_used.items()):
            cap = role_cap.get(r, 0.0)
            if used > cap:
                result.diagnostics.append(Diagnostic(
                    "fighter_role_slots_exceeded", Severity.ERROR,
                    "Not enough fighter slots for this role",
                    detail=f"{r}: {used} of {int(cap)}", contextual=False,
                    suggested_action="Launch fewer squadrons of this role.",
                    params={"role": r, "used": used, "cap": int(cap)}))
    if squadrons and is_carrier and bay_used > bay_cap:
        result.diagnostics.append(Diagnostic(
            "fighter_bay_exceeded", Severity.ERROR, "Fighter bay volume exceeded",
            detail=f"{round(bay_used, 1)} of {round(bay_cap, 1)} m3", contextual=False,
            suggested_action="Carry fewer or smaller fighters.",
            params={"used": round(bay_used, 1), "cap": round(bay_cap, 1)}))

    section = {
        "squadrons": squadrons,
        "totals": {
            "fighter_dps": round(fighter_dps, 1),
            "volley": round(fighter_volley_total, 1),
            "tubes_used": tubes_used, "tubes_total": int(tubes_cap),
            "bay_used_m3": round(bay_used, 1), "bay_capacity_m3": round(bay_cap, 1),
            "role_slots": {r: {"used": role_used[r], "total": int(role_cap.get(r, 0.0))}
                           for r in sorted(role_used)},
        },
    }
    agg = {"dps": fighter_dps, "volley": fighter_volley_total, "by_type": by_type_total,
           "sustained": fighter_dps, "has_damage": fighter_dps > 0}
    return section, agg


# --------------------------------------------------------------------------- #
# Exotic weapons (WS-9): smartbombs, vorton projectors, breacher pods
# --------------------------------------------------------------------------- #
def _smartbomb_entry(ev: EvaluatedFit, m: Entity, provider, target):
    """A smartbomb: an area pulse dealing its own em/th/kin/exp damage to everything within
    empFieldRange every cycle. Damage lives on the MODULE (no charge), so there is no magazine
    and sustained == burst. Being an area effect it auto-hits anything in range, so applied ==
    raw with an ``aoe`` note (documented in the matrix: no single-target tracking applies)."""
    shot = {d: ev.value(m, A.CHARGE_DAMAGE[d]) if A.CHARGE_DAMAGE[d] in m.base else 0.0
            for d in A.DAMAGE_TYPES}
    volley = sum(shot.values())
    cycle_s = ev.value(m, ATTR_DURATION) / 1000.0
    dps = volley / cycle_s if cycle_s > 0 else 0.0
    info = provider.type_info(m.type_id) or {}
    entry = {
        "type_id": m.type_id, "name": info.get("name", ""), "kind": "smartbomb",
        "volley": round(volley, 1), "dps": round(dps, 1),
        "range_m": round(ev.value(m, ATTR_EMP_FIELD_RANGE), 0),
        # No charge → no magazine → never reloads → sustained equals burst.
        "magazine_shots": None, "time_to_empty_s": None, "reload_s": None,
        "sustained_dps": round(dps, 1),
        "_by_type": {d: (shot[d] / cycle_s if cycle_s > 0 else 0.0)
                     for d in A.DAMAGE_TYPES},
    }
    if target is not None:
        entry["applied_dps"] = round(dps, 1)
        entry["applied_multiplier"] = 1.0
        entry["applied_note"] = "aoe"
    return entry, dps, volley


def _vorton_entry(ev: EvaluatedFit, m: Entity, provider, target):
    """A vorton projector: fires a Condenser Pack that detonates on the primary target for the
    charge's damage × the projector's damageMultiplier, then arcs to nearby secondaries. Only
    the PRIMARY-target hit is modelled in v1 — the arc to arc_targets within arc_range is
    reported for context but its extra damage is NOT counted (documented in the matrix). The
    projector carries the missile-style AoE attributes, so applied damage uses the same
    explosion size/velocity formula the missile path uses. Returns ``(None, 0, 0)`` if the
    charge yields no damage."""
    ch = m.charge
    shot = {d: ev.value(ch, A.CHARGE_DAMAGE[d]) if A.CHARGE_DAMAGE[d] in ch.base else 0.0
            for d in A.DAMAGE_TYPES}
    shot_total = sum(shot.values())
    dmg_mult = ev.value(m, A.DAMAGE_MULTIPLIER) if A.DAMAGE_MULTIPLIER in m.base else 1.0
    rof_s = ev.value(m, A.RATE_OF_FIRE) / 1000.0
    if rof_s <= 0 or shot_total <= 0:
        return None, 0.0, 0.0
    volley = shot_total * dmg_mult
    dps = volley / rof_s
    info = provider.type_info(m.type_id) or {}
    entry = {
        "type_id": m.type_id, "name": info.get("name", ""), "kind": "vorton",
        "volley": round(volley, 1), "dps": round(dps, 1),
        "range_m": round(ev.value(m, A.OPTIMAL_RANGE), 0),
        "arc_range_m": (round(ev.value(m, ATTR_VORTON_ARC_RANGE), 0)
                        if ATTR_VORTON_ARC_RANGE in m.base else None),
        "arc_targets": (int(ev.value(m, ATTR_VORTON_ARC_TARGETS))
                        if ATTR_VORTON_ARC_TARGETS in m.base else None),
        "_by_type": {d: (shot[d] * dmg_mult) / rof_s for d in A.DAMAGE_TYPES},
    }
    if target is not None:
        # AoE application attrs live on the projector MODULE (not the charge, unlike missiles).
        factor = _missile_application(
            target.signature_radius, target.velocity,
            ev.value(m, A.AOE_CLOUD_SIZE), ev.value(m, A.AOE_VELOCITY),
            ev.value(m, A.AOE_DAMAGE_REDUCTION_FACTOR))
        entry["applied_dps"] = round(dps * factor, 1)
        entry["applied_multiplier"] = round(factor, 3)
    sustained_fields, _sustained = _sustained_entry(ev, m, volley, dps, rof_s)
    entry.update(sustained_fields)
    return entry, dps, volley


def _breacher_entry(ev: EvaluatedFit, m: Entity, provider, target):
    """A breacher pod launcher: the pod attaches a non-stacking damage-over-time. Each 1-second
    tick deals ``min(flat_tick, pct_tick% × target_total_HP)``; without the target's HP only the
    flat arm is known. The pod refires (RoF) before its DoT (dot_duration_s) expires and DoTs do
    not stack, so one launcher keeps the DoT continuously up → its dps and sustained both equal
    the per-tick damage. Returns ``(entry, contribution)`` where ``contribution`` carries the
    raw / applied / sustained dps the caller caps across launchers (non-stacking)."""
    ch = m.charge
    flat_tick = (ev.value(ch, ATTR_DOT_MAX_DAMAGE_PER_TICK)
                 if ATTR_DOT_MAX_DAMAGE_PER_TICK in ch.base else 0.0)
    pct_tick = (ev.value(ch, ATTR_DOT_MAX_HP_PCT_PER_TICK)
                if ATTR_DOT_MAX_HP_PCT_PER_TICK in ch.base else 0.0)
    dot_duration_s = ((ev.value(ch, ATTR_DOT_DURATION) / 1000.0)
                      if ATTR_DOT_DURATION in ch.base else 0.0)
    flat_dps = flat_tick / _DOT_TICK_S
    info = provider.type_info(m.type_id) or {}
    entry = {
        "type_id": m.type_id, "name": info.get("name", ""), "kind": "breacher",
        "flat_tick": round(flat_tick, 1),
        "pct_tick_of_max_hp": round(pct_tick, 3),
        "dot_duration_s": round(dot_duration_s, 1),
        # Raw (unapplied) headline is the flat arm; dps counts the flat arm toward total_dps.
        "dot_dps": round(flat_dps, 1),
        "dps": round(flat_dps, 1),
        "volley": round(flat_tick, 1),                # one tick's damage
    }
    applied_dps = flat_dps
    if target is not None:
        if target.target_hp is not None and target.target_hp > 0:
            pct_arm = (pct_tick / 100.0) * target.target_hp
            tick = min(flat_tick, pct_arm) if flat_tick > 0 else pct_arm
            applied_dps = tick / _DOT_TICK_S
            entry["applied_dps"] = round(applied_dps, 1)
            entry["dot_dps"] = round(applied_dps, 1)  # both arms resolved with known HP
        else:
            entry["applied_dps"] = round(flat_dps, 1)
            entry["applied_reason"] = "target_hp_unknown"
    # The DoT is continuous while ammo lasts (refreshes each RoF, never gaps), so sustained ==
    # the raw dot dps. The pod magazine + reload are reported for reference only.
    shots = _magazine_shots(ev, m)
    entry["magazine_shots"] = shots or None
    entry["reload_s"] = (round(ev.value(m, ATTR_RELOAD_TIME) / 1000.0, 1)
                         if ATTR_RELOAD_TIME in m.base else None)
    entry["sustained_dps"] = round(flat_dps, 1)
    return entry, {"dps": flat_dps, "applied": applied_dps, "sustained": flat_dps}


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


def _ewar_kind(m: Entity) -> str | None:
    """Classify a module as an offensive-ewar family by its DEFAULT (identifying) effect id,
    or ``None`` when it is not ewar. Effect ids are stable across metalevels and avoid the
    group-id / shared-attribute pitfalls the old readout hit (see _EWAR_EFFECT_KIND)."""
    for eid, kind in _EWAR_EFFECT_KIND.items():
        if eid in m.effect_ids:
            return kind
    return None


def _ewar_strengths(ev: EvaluatedFit, m: Entity, kind: str) -> dict:
    """The evaluated bonus attribute(s) that define this family's strength (post-skills,
    post-overload). Attribute ids verified live 2026-07-21 (scout-data §D + type dumps)."""
    def val(attr: int) -> float:
        return round(ev.value(m, attr), 2) if attr in m.base else 0.0
    if kind in ("ecm", "ecm_burst"):
        return {k: val(v) for k, v in A.ECM_STRENGTH.items()}
    if kind == "sensor_dampener":
        return {"lock_range_bonus": val(A.MAX_TARGET_RANGE_BONUS),
                "scan_res_bonus": val(A.SCAN_RESOLUTION_BONUS)}
    if kind == "target_painter":
        return {"signature_bonus": val(A.SIGNATURE_RADIUS_BONUS_ATTR)}
    if kind == "stasis_web":
        return {"speed_bonus": val(A.SPEED_BONUS)}
    if kind == "warp_disruption":
        return {"warp_scramble_strength": val(A.WARP_SCRAMBLE_STRENGTH)}
    if kind == "tracking_disruptor":
        return {"tracking_bonus": val(A.TRACKING_SPEED_BONUS),
                "optimal_bonus": val(A.TD_MAX_RANGE_BONUS),
                "falloff_bonus": val(A.TD_FALLOFF_BONUS)}
    if kind == "guidance_disruptor":
        return {"missile_velocity_bonus": val(A.MISSILE_VELOCITY_BONUS),
                "explosion_delay_bonus": val(A.EXPLOSION_DELAY_BONUS),
                "explosion_velocity_bonus": val(A.AOE_VELOCITY_BONUS),
                "explosion_radius_bonus": val(A.AOE_CLOUD_SIZE_BONUS)}
    if kind == "energy_neutraliser":
        return {"amount_gj": val(A.ENERGY_NEUTRALISER_AMOUNT)}
    if kind == "nosferatu":
        return {"amount_gj": val(A.POWER_TRANSFER_AMOUNT)}
    return {}


def _ewar_entry(ev: EvaluatedFit, m: Entity, kind: str, provider) -> dict:
    info = provider.type_info(m.type_id) or {}
    cyc = _cycle_ms(ev, m) / 1000.0
    entry = {
        "type_id": m.type_id, "name": info.get("name", ""), "kind": kind,
        "strengths": _ewar_strengths(ev, m, kind),
        "cycle_s": round(cyc, 2) if cyc > 0 else None,
        "cap_per_cycle": round(ev.value(m, A.CAP_NEED), 1) if A.CAP_NEED in m.base else 0.0,
    }
    if kind == "ecm_burst":
        # A burst jammer is an AoE self-effect: no optimal/falloff, a single burst radius.
        entry["optimal_m"] = None
        entry["falloff_m"] = None
        entry["burst_range_m"] = (round(ev.value(m, A.ECM_BURST_RANGE), 0)
                                  if A.ECM_BURST_RANGE in m.base else None)
    else:
        entry["optimal_m"] = round(ev.value(m, A.OPTIMAL_RANGE), 0)
        entry["falloff_m"] = round(ev.value(m, A.FALLOFF), 0)
    if kind in ("energy_neutraliser", "nosferatu"):
        amt = entry["strengths"]["amount_gj"]
        entry["per_second"] = round(amt / cyc, 1) if cyc > 0 else 0.0
    return entry


def _attach_jam(entry: dict, ev: EvaluatedFit, m: Entity, tgt_ss, tgt_sensor,
                jammer_ps: list) -> None:
    """ECM jam chance = jammer strength(238-241) / target sensor strength, clamped [0,1]
    (pyfa eos/effects.py:30992 uses scanXStrengthBonus for the target's sensor type; the
    combining maths lives in _jam_summary). Absent target sensor strength → null + reason
    (never a fabricated number)."""
    strengths = {k: ev.value(m, v) if v in m.base else 0.0
                 for k, v in A.ECM_STRENGTH.items()}
    if not tgt_ss or tgt_ss <= 0:
        entry["jam_chance"] = None
        entry["jam_reason"] = "target_sensor_strength_unknown"
        return
    if tgt_sensor in strengths:
        p = min(1.0, strengths[tgt_sensor] / tgt_ss)
        entry["jam_chance"] = round(p, 4)
        entry["jam_sensor"] = tgt_sensor
        jammer_ps.append(p)
    else:
        # No sensor type named: report the per-type chance for each racial sensor.
        entry["jam_chances"] = {k: round(min(1.0, v / tgt_ss), 4)
                                for k, v in strengths.items()}


def _jam_summary(tgt_ss, tgt_sensor, jammer_ps: list, jammer_count: int) -> dict:
    """Combined jam chance across all our jammers as independent per-cycle rolls:
    ``1 − Π(1 − p_i)`` (pyfa eos/saveddata/fit.py:439-444 jamChance — retainLockChance is the
    product of each jammer's miss chance). Null with a reason when the target's sensor
    strength or the sensor type is not supplied."""
    summary = {"target_sensor_strength": tgt_ss, "target_sensor_type": tgt_sensor,
               "jammer_count": jammer_count, "combined_chance": None, "reason": None}
    if not tgt_ss or tgt_ss <= 0:
        summary["reason"] = "target_sensor_strength_unknown"
    elif tgt_sensor not in _SENSOR_TYPES:
        summary["reason"] = "no_target_sensor_type"
    else:
        retain = 1.0
        for p in jammer_ps:
            retain *= 1.0 - p
        summary["combined_chance"] = round(1.0 - retain, 4)
    return summary


def _stacked_postpercent(base: float, bonuses: list) -> float:
    """``base`` after a set of postPercent bonuses, stacking-penalised exactly like
    graph._calculate (sort by |bonus| descending; the i-th multiplies by
    ``1 + bonus/100 · penalty^(i²)``). Used to fold our painters/webs/damps onto the target
    profile with the SAME maths a fitted/projected module modifier uses."""
    val = base
    for i, b in enumerate(sorted(bonuses, key=abs, reverse=True)):
        val *= 1.0 + (b / 100.0) * (_EWAR_PENALTY ** (i * i))
    return val


def _ewar_target_adjustment(ev: EvaluatedFit, op_profile: OperatingProfile):
    """Apply OUR active painters/webs/damps to the operating profile's target, returning
    ``(adjusted_target | None, ewar_on_target | None)``. Painters enlarge the target's
    signature, webs slow it (both stacking-penalised among our own family) — the physical
    effect the applied DPS is then measured against. Damps modify the target's lock range /
    scan resolution, which do not feed applied DPS, so they are reported as deltas only.
    ``adjusted_target`` is ``None`` when nothing changed sig/velocity (so _offence reuses the
    raw target); ``ewar_on_target`` is ``None`` when there is no target to measure against."""
    target = op_profile.target
    if target is None:
        return None, None
    painters: list[float] = []
    webs: list[float] = []
    lock_bonuses: list[float] = []
    scan_bonuses: list[float] = []
    for m in ev.modules:
        if not _active(m):
            continue
        kind = _ewar_kind(m)
        if kind == "target_painter":
            painters.append(ev.value(m, A.SIGNATURE_RADIUS_BONUS_ATTR))
        elif kind == "stasis_web":
            webs.append(ev.value(m, A.SPEED_BONUS))
        elif kind == "sensor_dampener":
            lock_bonuses.append(ev.value(m, A.MAX_TARGET_RANGE_BONUS))
            scan_bonuses.append(ev.value(m, A.SCAN_RESOLUTION_BONUS))
    base_sig, base_vel = target.signature_radius, target.velocity
    adj_sig = _stacked_postpercent(base_sig, painters) if painters else base_sig
    adj_vel = _stacked_postpercent(base_vel, webs) if webs else base_vel
    on_target = {
        "base": {"signature": round(base_sig, 1), "velocity": round(base_vel, 1)},
        "adjusted": {"signature": round(adj_sig, 1), "velocity": round(adj_vel, 1)},
        "painter_sig_pct": round((adj_sig / base_sig - 1.0) * 100, 1) if base_sig > 0 else 0.0,
        "web_velocity_pct": round((adj_vel / base_vel - 1.0) * 100, 1) if base_vel > 0 else 0.0,
    }
    if lock_bonuses or scan_bonuses:
        on_target["damp"] = {
            "lock_range_pct": round((_stacked_postpercent(1.0, lock_bonuses) - 1.0) * 100, 1),
            "scan_res_pct": round((_stacked_postpercent(1.0, scan_bonuses) - 1.0) * 100, 1),
        }
    changed = (bool(painters) and base_sig > 0) or (bool(webs) and base_vel > 0)
    if not changed:
        return None, on_target
    # A web reduces the target's DERIVED angular (via the lower velocity); an explicitly
    # pinned target_angular is respected as-is (the pilot's override wins) — documented.
    adjusted = replace(target, signature_radius=adj_sig, velocity=adj_vel)
    return adjusted, on_target


def _ewar(ev: EvaluatedFit, op_profile: OperatingProfile, on_target, provider) -> dict:
    """Consolidated EWAR telemetry: one entry per OUR active offensive-ewar module (jammers,
    burst jammers, dampeners, painters, webs, scramblers/disruptors, tracking/guidance
    disruptors; neut/nos cross-linked from the capacitor section). Adds ECM jam chances when
    the target's sensor strength is supplied, and the ewar-adjusted target profile."""
    target = op_profile.target
    tgt_ss = target.target_sensor_strength if target is not None else None
    tgt_sensor = target.target_sensor_type if target is not None else None
    entries: list[dict] = []
    jammer_ps: list[float] = []
    jammer_count = 0
    for m in ev.modules:
        if not _active(m):
            continue
        kind = _ewar_kind(m)
        if kind is None:
            continue
        entry = _ewar_entry(ev, m, kind, provider)
        if kind in ("ecm", "ecm_burst"):
            jammer_count += 1
            _attach_jam(entry, ev, m, tgt_ss, tgt_sensor, jammer_ps)
        entries.append(entry)
    section = {"modules": entries, "count": len(entries)}
    if jammer_count:
        section["jam"] = _jam_summary(tgt_ss, tgt_sensor, jammer_ps, jammer_count)
    if on_target is not None:
        section["ewar_on_target"] = on_target
    return section


# --------------------------------------------------------------------------- #
# Mining yield (WS-8): ore / ice / gas laser + mining-drone telemetry
# --------------------------------------------------------------------------- #
def _mining_kind(m: Entity) -> str:
    """Classify a mining module as ore / ice / gas from its data (never by name).

    * gas  — carries the miningClouds effect (a gas cloud harvester).
    * ice  — requires the Ice Harvesting skill (an ice harvester; shares miningLaser with
             ore lasers, so the required skill is the only data-driven discriminator).
    * ore  — every other mining laser.
    """
    if EFFECT_MINING_CLOUDS in m.effect_ids:
        return "gas"
    for attr in A.REQUIRED_SKILL_ATTRS:
        if m.base.get(attr) == float(SKILL_ICE_HARVESTING):
            return "ice"
    return "ore"


def _mining_entry(ev: EvaluatedFit, ent: Entity, provider, kind: str) -> dict | None:
    """One industry row for a mining module or drone. ``yield_per_cycle`` is the evaluated
    miningAmount(77) — m³ of ore/gas per cycle, or the m³ of one ice block per cycle for an
    ice harvester (CCP counts an ice cycle as a single 1000 m³ block). Residue (mining-waste)
    fields are surfaced only when the module carries miningWasteProbability(3154)."""
    yield_per_cycle = ev.value(ent, ATTR_MINING_AMOUNT)
    cycle_s = ev.value(ent, ATTR_DURATION) / 1000.0
    if yield_per_cycle <= 0 or cycle_s <= 0:
        return None
    per_hour = yield_per_cycle / cycle_s * 3600.0
    info = provider.type_info(ent.type_id) or {}
    entry = {
        "type_id": ent.type_id,
        "name": info.get("name", ""),
        "kind": kind,
        "yield_per_cycle": round(yield_per_cycle, 1),
        "cycle_s": round(cycle_s, 2),
        "m3_per_hour": round(per_hour, 1),
    }
    if ent.quantity > 1:
        entry["quantity"] = ent.quantity
    # Residue / mining-waste (a chance to lose part of the mined volume). Present on modulated
    # strip miners, ice/gas harvesters and mining drones; absent on basic ore lasers.
    if ATTR_MINING_WASTE_PROBABILITY in ent.base:
        entry["waste_probability"] = round(ev.value(ent, ATTR_MINING_WASTE_PROBABILITY), 1)
        entry["waste_volume_multiplier"] = round(
            ev.value(ent, ATTR_MINING_WASTE_VOLUME_MULT), 3)
    return entry, per_hour


def _industry(ev: EvaluatedFit, provider) -> dict | None:
    """Mining telemetry, or ``None`` when the fit has no mining modules/drones.

    A mining module is an ACTIVE high-slot laser carrying the miningLaser or miningClouds
    effect; a mining drone carries the mining effect. Each entry's per-cycle yield and cycle
    time come straight from the evaluated graph (so hull bonuses and loaded crystals are
    already folded in). ``by_kind`` gives m³/hour subtotals for the kinds actually present.
    """
    modules: list[dict] = []
    per_hour_by_kind: dict[str, float] = {}

    def _add(ent: Entity, kind: str):
        made = _mining_entry(ev, ent, provider, kind)
        if made is None:
            return
        entry, per_hour = made
        modules.append(entry)
        per_hour_by_kind[kind] = per_hour_by_kind.get(kind, 0.0) + per_hour

    for m in ev.modules:
        if m.slot != SlotKind.HIGH or not _active(m):
            continue
        if _MINING_MODULE_EFFECTS & m.effect_ids:
            _add(m, _mining_kind(m))
    for dr in ev.drones:
        if EFFECT_MINING_DRONE in dr.effect_ids or ATTR_MINING_AMOUNT in dr.base:
            # A drone entity carries ``quantity`` fielded drones; yield scales with it.
            made = _mining_entry(ev, dr, provider, "drone")
            if made is None:
                continue
            entry, per_hour = made
            per_hour *= dr.quantity
            entry["m3_per_hour"] = round(per_hour, 1)
            modules.append(entry)
            per_hour_by_kind["drone"] = per_hour_by_kind.get("drone", 0.0) + per_hour

    if not modules:
        return None
    return {
        "modules": modules,
        "m3_per_hour_total": round(sum(per_hour_by_kind.values()), 1),
        "by_kind": {k: round(v, 1) for k, v in per_hour_by_kind.items()},
    }


# --------------------------------------------------------------------------- #
# Projected effects (WS-6): what hostile modules are pressuring this fit
# --------------------------------------------------------------------------- #
def _projected_summary(ev: EvaluatedFit, p: Entity) -> str:
    """A short human description of what one projected module does to us, from its own
    evaluated (base) attributes."""
    eff = p.effect_ids
    cyc = ev.value(p, ATTR_DURATION)
    if EFFECT_PROJ_WEB in eff:
        return f"{ev.value(p, ATTR_SPEED_FACTOR):.0f}% max velocity"
    if EFFECT_PROJ_PAINT in eff:
        return f"+{ev.value(p, ATTR_SIG_RADIUS_BONUS):.0f}% signature radius"
    if EFFECT_PROJ_DAMP in eff:
        return (f"{ev.value(p, ATTR_MAX_TGT_RANGE_BONUS):.0f}% lock range, "
                f"{ev.value(p, ATTR_SCAN_RES_BONUS):.0f}% scan resolution")
    if EFFECT_PROJ_SCRAM in eff:
        return f"{ev.value(p, ATTR_WARP_SCRAMBLE_STRENGTH):.0f} warp scramble strength"
    if EFFECT_PROJ_NEUT in eff or EFFECT_PROJ_NOS in eff:
        is_neut = EFFECT_PROJ_NEUT in eff
        amt = ev.value(p, ATTR_ENERGY_NEUT_AMOUNT if is_neut else ATTR_POWER_TRANSFER_AMOUNT)
        gjs = amt / (cyc / 1000.0) if cyc > 0 else 0.0
        return f"{'neutralises' if is_neut else 'nosferatu drains'} {gjs:.1f} GJ/s"
    for layer, e, attr in _PROJ_REP_LAYERS:
        if e in eff:
            hps = ev.value(p, attr) / (cyc / 1000.0) if cyc > 0 else 0.0
            return f"remote {layer} rep {hps:.0f} HP/s"
    return "no modelled projected effect"


def _projected(ev: EvaluatedFit, provider) -> dict:
    """Telemetry list of the hostile modules projected onto this fit. Quantity-N inputs are
    materialised as N sources (for stacking); collapse them back to one row per (type,
    state) with a count so the UI shows "Stasis Webifier II ×2"."""
    groups: dict[tuple, dict] = {}
    order: list[tuple] = []
    for p in ev.projected:
        key = (p.type_id, p.module_state)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"entity": p, "count": 0}
            order.append(key)
        g["count"] += 1
    modules = []
    for key in order:
        p = groups[key]["entity"]
        info = provider.type_info(p.type_id) or {}
        modules.append({
            "type_id": p.type_id,
            "name": info.get("name", ""),
            "state": p.module_state.value if p.module_state else "active",
            "quantity": groups[key]["count"],
            "effect_summary": _projected_summary(ev, p),
        })
    return {"modules": modules, "count": len(modules)}


def _validate_projected(ev: EvaluatedFit, provider, result: FittingResult) -> None:
    """A projected module carrying NO target (category-2) effect cannot pressure this fit
    (e.g. a Gyrostabilizer dragged into the projected slot). Flag it — advisory WARNING, not
    a structural error — so a mis-picked module is visible rather than silently ignored."""
    seen: set[int] = set()
    for p in ev.projected:
        if p.type_id in seen:
            continue
        seen.add(p.type_id)
        has_target = False
        for eid in p.effect_ids:
            edef = provider.effect_def(eid)
            if edef is not None and edef.category == 2:
                has_target = True
                break
        if has_target:
            continue
        info = provider.type_info(p.type_id) or {}
        result.diagnostics.append(Diagnostic(
            "projected_module_inert", Severity.WARNING,
            "Projected module has no effect on this ship",
            detail=f"type {p.type_id}", contextual=True,
            params={"type_id": p.type_id, "name": info.get("name", "")}))


# WS-11: our data has no metaGroup column (apps.sde.models.SdeType), so an abyssal (mutated-
# instance) type is identified by its NAME — every mutated module's fittable SdeType is a
# distinct "Abyssal <base module>" row (verified: all 46 name__istartswith="Abyssal" types,
# scout-data §K). The fittable ones carry only structural base attrs (mass/volume/skill); the
# rolled combat attributes live entirely in a per-item override we cannot derive from the type.
_ABYSSAL_NAME_PREFIX = "abyssal"


def _validate_mutated(fit: FitInput, provider, result: FittingResult) -> None:
    """WS-11: an abyssal (mutated) module fitted WITHOUT its rolled attribute overrides has no
    real stats to show — its SdeType carries only structural attrs, so the engine evaluates it
    at base-roll (a Gyrostabilizer that multiplies damage by the dogma default 1.0, i.e. does
    nothing). Flag that honestly — advisory WARNING, non-structural (the fit is still valid) —
    so a base-roll abyssal module is visible rather than silently contributing wrong numbers. A
    module that DOES carry overrides (including a non-abyssal hull mutated pyfa-style — tolerated
    input) is silent. ESI killmails never carry mutated attributes, so an imported abyssal loss
    legitimately trips this warning."""
    seen: set[int] = set()
    for m in fit.modules:
        if m.slot == SlotKind.CARGO or m.attr_overrides or m.type_id in seen:
            continue
        info = provider.type_info(m.type_id) or {}
        name = (info.get("name") or "").strip()
        if not name.lower().startswith(_ABYSSAL_NAME_PREFIX):
            continue
        seen.add(m.type_id)
        result.diagnostics.append(Diagnostic(
            "mutated_attributes_unknown", Severity.WARNING,
            "Mutated module — rolled attributes unknown",
            detail=f"type {m.type_id}", contextual=False,
            params={"type_id": m.type_id, "name": name}))


# --------------------------------------------------------------------------- #
# Fleet boosts (WS-7): friendly command bursts boosting this fit
# --------------------------------------------------------------------------- #
def _boosts(ev: EvaluatedFit, provider, result: FittingResult) -> dict:
    """Telemetry for the friendly command bursts boosting this fit, from the per-boost record
    the graph produced (``ev.boosts_applied``). Attaches each burst charge's name and raises
    ``boost_unknown_buff`` (advisory) for any charge that references a warfare buff id absent
    from the imported dbuff table — so a burst whose buff FORCA cannot model is visible rather
    than silently doing nothing. The buff attribute maths already happened in the graph; this
    is display + honesty only."""
    boosts = []
    warned: set[tuple[int, int]] = set()
    for rec in ev.boosts_applied:
        cid = rec["charge_type_id"]
        info = provider.type_info(cid) or {}
        boosts.append({"charge_type_id": cid, "name": info.get("name", ""),
                       "buffs": rec["buffs"]})
        for buff in rec["buffs"]:
            key = (cid, buff["buff_id"])
            if buff["applied"] or key in warned:
                continue
            warned.add(key)
            result.diagnostics.append(Diagnostic(
                "boost_unknown_buff", Severity.WARNING,
                "Command burst references an unknown warfare buff",
                detail=f"charge {cid} buff {buff['buff_id']}", contextual=True,
                params={"charge_type_id": cid, "name": info.get("name", ""),
                        "buff_id": buff["buff_id"]}))
    return {"boosts": boosts, "count": len(boosts)}


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
    for f in fit.fighters:
        type_ids.add(f.type_id)   # Fighters / Light|Heavy Fighters / racial spec skills
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
                  "booster_slot_conflict", "mode_invalid_for_ship",
                  "fighter_tubes_exceeded", "fighter_role_slots_exceeded",
                  "fighter_squadron_oversized", "fighter_bay_exceeded",
                  "fighter_on_non_carrier", "fighter_invalid_type"}
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
