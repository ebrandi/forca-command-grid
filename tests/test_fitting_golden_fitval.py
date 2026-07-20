"""Golden fits: fitting VALIDATION (engine v2, real SDE slices, hand-derived numbers).

Domain: CPU/PG/calibration budgets and their rescuers, fitting-skill CPU reduction,
per-rack slot limits, maxGroupFitted, rig size, charge group/size compatibility,
drone bay/bandwidth, the status precedence ladder, T3 subsystem slot folding with a
subsystem-skill bonus, and offline modules freeing CPU/PG.

Fixtures are real CCP data slices (tests/fixtures/fitting/fitval_*.json) extracted
through scripts/tochas_lab_extract_fixture.py from the live dev SDE import. Every
expected value is DERIVED IN THIS FILE from the slice's base attributes plus documented
EVE mechanics; skill percentages are read from the skill's own dogma attributes in the
DB and multiplied by the trained level. Nothing is read back from the engine.

Mechanics used (all verified against the live SDE dogma rows, cited inline):
* CPU Management (3426): +5%/level ship CPU output — skill attr 424, effect 397
  (postPercent ship attr 48).
* Power Grid Management (3413): +5%/level ship powergrid — skill attr 313, effect 490.
* Weapon Upgrades (3318): -5%/level CPU need of modules requiring Gunnery (3300) —
  skill attr 310, effect 581 (postPercent module attr 50).
* Advanced Weapon Upgrades (11207): -2%/level powergrid need of modules requiring
  Gunnery — skill attr 323, effect 1638 (postPercent module attr 30).
* Co-Processor II: xcpuMultiplier (attr 202 = 1.1) on ship CPU output, effect 536.
* Reactor Control Unit II: xpowerOutputMultiplier (attr 145 = 1.15), effect 56.
* Micro Auxiliary Power Core I: +powerIncrease (attr 549 = 10 MW) modAdd, effect 627.
* Loki subsystems: slot/hardpoint folding from attrs 1374/1375/1376/1368/1369 (the
  slotModifier effect carries no dogma modifiers; the engine folds them as a documented
  patch), CPU/PG/drone bw/bay/cap/HP adds via modAdd effects 3782/3783/3797/3799/3811/
  3831/3771/6920.
* Minmatar Core Systems (30547): scales the Core subsystem's subsystemBonusMinmatarCore
  (attr 1446 = -5, % capacitor recharge time per level) via effect 3843 (preMul by
  skill level attr 280); the subsystem applies it postPercent to ship attr 55
  (effect 4264).
"""
from __future__ import annotations

import pytest

from apps.fitting.engine import attributes as A
from apps.fitting.engine.types import ModuleInput, ModuleState, SkillProfile, SlotKind

from ._fitting_graph_utils import evaluate_fit, load_graph_fixture

pytestmark = pytest.mark.django_db

# Skill type ids (CCP SDE), present in the shared skills_core slice.
CPU_MANAGEMENT = 3426
POWER_GRID_MANAGEMENT = 3413
WEAPON_UPGRADES = 3318
ADV_WEAPON_UPGRADES = 11207
MINMATAR_CORE_SYSTEMS = 30547

# Named dogma attributes used by the derivations below.
ATTR_CPU_OUTPUT_BONUS = 424      # cpuOutputBonus2 (CPU Management, %/level)
ATTR_CPU_NEED_BONUS = 310        # cpuNeedBonus (Weapon Upgrades, %/level)
ATTR_POWER_NEED_BONUS = 323      # powerNeedBonus (Advanced Weapon Upgrades, %/level)
ATTR_CPU_MULTIPLIER = 202        # cpuMultiplier (Co-Processor)
ATTR_PG_MULTIPLIER = 145         # powerOutputMultiplier (RCU/PDS)
ATTR_PG_INCREASE = 549           # powerIncrease (MAPC, flat MW)
ATTR_CALIBRATION_COST = 1153     # upgradeCost (rigs)
ATTR_RIG_SIZE = 1547
ATTR_MAX_GROUP_FITTED = 1544
ATTR_DRONE_BW_USED = 1272
ATTR_VOLUME = 161
ATTR_CAP_RECHARGE = 55
ATTR_CAP_CAPACITY = 482
ATTR_SUB_CORE_BONUS = 1446       # subsystemBonusMinmatarCore (%/level, cap recharge)
ATTR_SUB_HI = 1374
ATTR_SUB_MED = 1375
ATTR_SUB_LOW = 1376
ATTR_SUB_TURRET = 1368
ATTR_SUB_LAUNCHER = 1369
ATTR_ARMOR_PLATE_BONUS = 1159    # armorHPBonusAdd (Defensive subsystem flat armor add)
ATTR_STRUCTURE_ADD = 2688        # structureHPBonusAdd (Defensive subsystem)


@pytest.fixture()
def rifter_ids():
    return load_graph_fixture("fitval_rifter")


@pytest.fixture()
def tristan_ids():
    return load_graph_fixture("fitval_tristan")


@pytest.fixture()
def loki_ids():
    return load_graph_fixture("fitval_loki")


def _attr(type_id, attr_id):
    from apps.sde.models import SdeTypeAttribute
    return SdeTypeAttribute.objects.get(type_id=type_id, attribute_id=attr_id).value


def _group(type_id):
    from apps.sde.models import SdeType
    return SdeType.objects.get(type_id=type_id).group_id


def _codes(res):
    return {d.code for d in res.diagnostics}


def _diag(res, code):
    hits = [d for d in res.diagnostics if d.code == code]
    assert hits, f"expected diagnostic {code}, got {sorted(_codes(res))}"
    return hits[0]


def _mod(tid, slot, state=ModuleState.ONLINE, charge=None):
    return ModuleInput(type_id=tid, slot=slot, state=state, charge_type_id=charge)


# --------------------------------------------------------------------------- #
# 1. CPU-limited fit rescued by a Co-Processor II (realistic, All V)
# --------------------------------------------------------------------------- #
def test_cpu_limited_fit_rescued_by_coprocessor(rifter_ids):
    ids = rifter_ids
    rifter = ids["Rifter"]
    gun, ammo = ids["150mm Light AutoCannon II"], ids["Republic Fleet EMP S"]
    mods = [ModuleInput(type_id=gun, slot=SlotKind.HIGH, state=ModuleState.ACTIVE,
                        charge_type_id=ammo) for _ in range(3)]
    mods += [_mod(ids["Warp Disruptor II"], SlotKind.MED),
             _mod(ids["Multispectrum Shield Hardener II"], SlotKind.MED),
             _mod(ids["Remote Sensor Dampener II"], SlotKind.MED),
             _mod(ids["Gyrostabilizer II"], SlotKind.LOW)]

    # CPU output at All V: base x (1 + 5*cpuOutputBonus2/100); CPU Management attr 424.
    cpu_mgmt = _attr(CPU_MANAGEMENT, ATTR_CPU_OUTPUT_BONUS)          # +5 %/level
    cpu_out = _attr(rifter, A.CPU_OUTPUT) * (1 + 5 * cpu_mgmt / 100.0)
    # Gun CPU at All V: Weapon Upgrades attr 310 (-5 %/level) on Gunnery modules.
    wu = _attr(WEAPON_UPGRADES, ATTR_CPU_NEED_BONUS)                 # -5 %/level
    gun_cpu = _attr(gun, A.CPU_USAGE) * (1 + 5 * wu / 100.0)
    # None of the med/low modules require Gunnery/Electronics Upgrades/Energy Grid
    # Upgrades/Shield Upgrades, so no All-V skill touches their CPU (verified in SDE).
    used = 3 * gun_cpu + sum(_attr(ids[n], A.CPU_USAGE) for n in (
        "Warp Disruptor II", "Multispectrum Shield Hardener II",
        "Remote Sensor Dampener II", "Gyrostabilizer II"))

    over = evaluate_fit(rifter, mods)                                # All V
    assert over.status.value == "over_resources"
    d = _diag(over, "cpu_exceeded")
    assert d.params["used"] == pytest.approx(used, rel=2e-3)
    assert d.params["cap"] == pytest.approx(cpu_out, rel=2e-3)
    assert over.telemetry["resources"]["cpu"]["used"] == pytest.approx(used, rel=2e-3)
    assert over.telemetry["resources"]["cpu"]["output"] == pytest.approx(cpu_out, rel=2e-3)

    # Rescue: Co-Processor II multiplies ship CPU output by attr 202 (= 1.1); its own
    # CPU need is 0 tf so the used total is unchanged.
    coproc = ids["Co-Processor II"]
    fixed = evaluate_fit(rifter, mods + [_mod(coproc, SlotKind.LOW)])
    fixed_out = cpu_out * _attr(coproc, ATTR_CPU_MULTIPLIER)
    fixed_used = used + _attr(coproc, A.CPU_USAGE)                   # + 0
    assert used > cpu_out and fixed_used <= fixed_out                # over -> fits
    assert "cpu_exceeded" not in _codes(fixed)
    assert fixed.status.value == "valid"
    assert fixed.telemetry["resources"]["cpu"]["used"] == pytest.approx(fixed_used, rel=2e-3)
    assert fixed.telemetry["resources"]["cpu"]["output"] == pytest.approx(fixed_out, rel=2e-3)

    # Powergrid cross-check: guns get AWU V (-2 %/level, attr 323); others are flat.
    awu = _attr(ADV_WEAPON_UPGRADES, ATTR_POWER_NEED_BONUS)          # -2 %/level
    pg_used = 3 * _attr(gun, A.POWER_USAGE) * (1 + 5 * awu / 100.0) + sum(
        _attr(ids[n], A.POWER_USAGE) for n in (
            "Warp Disruptor II", "Multispectrum Shield Hardener II",
            "Remote Sensor Dampener II", "Gyrostabilizer II", "Co-Processor II"))
    assert fixed.telemetry["resources"]["powergrid"]["used"] == pytest.approx(pg_used, rel=2e-3)


# --------------------------------------------------------------------------- #
# 2. PG-limited fit rescued by MAPC (flat add) and RCU (multiplier) — isolation
# --------------------------------------------------------------------------- #
def test_pg_limited_fit_rescued_by_mapc_and_rcu(rifter_ids):
    ids = rifter_ids
    rifter = ids["Rifter"]
    mods = [_mod(ids["Medium Shield Extender II"], SlotKind.MED),
            _mod(ids["1MN Afterburner II"], SlotKind.MED),
            _mod(ids["200mm AutoCannon II"], SlotKind.HIGH)]
    pg_out = _attr(rifter, A.POWER_OUTPUT)                           # 41 MW base
    pg_used = sum(_attr(ids[n], A.POWER_USAGE) for n in (
        "Medium Shield Extender II", "1MN Afterburner II", "200mm AutoCannon II"))
    assert pg_used > pg_out                                          # fixture sanity

    none = SkillProfile.from_dict({})
    over = evaluate_fit(rifter, mods, skills=none)
    assert over.status.value == "over_resources"
    d = _diag(over, "powergrid_exceeded")
    assert d.params["used"] == pytest.approx(pg_used, rel=2e-3)
    assert d.params["cap"] == pytest.approx(pg_out, rel=2e-3)

    # Rescue A: MAPC adds powerIncrease (attr 549) flat to ship powergrid (modAdd).
    mapc = ids["Micro Auxiliary Power Core I"]
    a = evaluate_fit(rifter, mods + [_mod(mapc, SlotKind.LOW)], skills=none)
    a_out = pg_out + _attr(mapc, ATTR_PG_INCREASE)
    assert pg_used + _attr(mapc, A.POWER_USAGE) <= a_out             # over -> fits
    assert "powergrid_exceeded" not in _codes(a)
    assert a.status.value == "missing_skills"                        # T2 mods, no skills
    assert a.telemetry["resources"]["powergrid"]["used"] == pytest.approx(
        pg_used + _attr(mapc, A.POWER_USAGE), rel=2e-3)
    assert a.telemetry["resources"]["powergrid"]["output"] == pytest.approx(a_out, rel=2e-3)

    # Rescue B: RCU II multiplies ship powergrid by powerOutputMultiplier (attr 145).
    rcu = ids["Reactor Control Unit II"]
    b = evaluate_fit(rifter, mods + [_mod(rcu, SlotKind.LOW)], skills=none)
    b_out = pg_out * _attr(rcu, ATTR_PG_MULTIPLIER)
    assert pg_used + _attr(rcu, A.POWER_USAGE) <= b_out
    assert "powergrid_exceeded" not in _codes(b)
    assert b.telemetry["resources"]["powergrid"]["output"] == pytest.approx(b_out, rel=2e-3)


# --------------------------------------------------------------------------- #
# 3. Weapon Upgrades reduces gun CPU need exactly (-5 %/level, skill attr 310)
# --------------------------------------------------------------------------- #
def test_weapon_upgrades_reduces_gun_cpu_exactly(rifter_ids):
    ids = rifter_ids
    rifter, gun = ids["Rifter"], ids["150mm Light AutoCannon II"]
    mods = [_mod(gun, SlotKind.HIGH) for _ in range(3)]
    base_cpu = _attr(gun, A.CPU_USAGE)                               # 6 tf
    wu = _attr(WEAPON_UPGRADES, ATTR_CPU_NEED_BONUS)                 # -5 (%/level)
    for level in (0, 3, 5):
        res = evaluate_fit(rifter, mods,
                           skills=SkillProfile.from_dict({WEAPON_UPGRADES: level}))
        expected = 3 * base_cpu * (1 + level * wu / 100.0)           # 18 / 15.3 / 13.5
        assert res.telemetry["resources"]["cpu"]["used"] == pytest.approx(
            expected, rel=2e-3)
        # Only Weapon Upgrades is trained: ship CPU output stays at hull base.
        assert res.telemetry["resources"]["cpu"]["output"] == pytest.approx(
            _attr(rifter, A.CPU_OUTPUT), rel=2e-3)


# --------------------------------------------------------------------------- #
# 4. Calibration overflow diagnostic
# --------------------------------------------------------------------------- #
def test_calibration_overflow(rifter_ids):
    ids = rifter_ids
    rifter = ids["Rifter"]
    r1, r2 = ids["Small Projectile Burst Aerator II"], \
        ids["Small Projectile Collision Accelerator II"]
    cal_out = _attr(rifter, A.CALIBRATION)                           # 400
    cal_used = _attr(r1, ATTR_CALIBRATION_COST) + _attr(r2, ATTR_CALIBRATION_COST)
    assert cal_used > cal_out                                        # 300 + 300 > 400
    res = evaluate_fit(rifter, [_mod(r1, SlotKind.RIG), _mod(r2, SlotKind.RIG)],
                       skills=SkillProfile.from_dict({}))
    d = _diag(res, "calibration_exceeded")
    assert d.params["used"] == pytest.approx(cal_used)
    assert d.params["cap"] == pytest.approx(cal_out)
    assert "rig_size_mismatch" not in _codes(res)                    # both size 1 rigs
    assert res.status.value == "over_resources"
    assert res.telemetry["resources"]["calibration"]["used"] == pytest.approx(cal_used)
    assert res.telemetry["resources"]["calibration"]["output"] == pytest.approx(cal_out)


# --------------------------------------------------------------------------- #
# 5. too_many_modules per rack
# --------------------------------------------------------------------------- #
def test_too_many_med_modules(rifter_ids):
    ids = rifter_ids
    rifter = ids["Rifter"]
    med_names = ("Medium Shield Extender II", "1MN Afterburner II",
                 "Warp Disruptor II", "Multispectrum Shield Hardener II")
    res = evaluate_fit(rifter, [_mod(ids[n], SlotKind.MED) for n in med_names],
                       skills=SkillProfile.from_dict({}))
    hull_med = int(_attr(rifter, A.MED_SLOTS))                       # 3
    assert len(med_names) > hull_med
    d = _diag(res, "too_many_modules")
    assert d.params == {"slot": "med", "used": len(med_names), "total": hull_med}
    assert res.status.value == "impossible"                          # structural wins
    slots = res.telemetry["resources"]["slots"]
    assert slots["used"]["med"] == len(med_names)
    assert slots["hull"]["med"] == hull_med


# --------------------------------------------------------------------------- #
# 6. max_group_fitted: two Damage Controls -> impossible
# --------------------------------------------------------------------------- #
def test_two_damage_controls_impossible(rifter_ids):
    ids = rifter_ids
    rifter, dcu = ids["Rifter"], ids["Damage Control II"]
    assert _attr(dcu, ATTR_MAX_GROUP_FITTED) == 1                    # SDE: maxGroupFitted
    res = evaluate_fit(rifter, [_mod(dcu, SlotKind.LOW), _mod(dcu, SlotKind.LOW)],
                       skills=SkillProfile.from_dict({}))
    d = _diag(res, "max_group_fitted")
    assert d.params == {"group_id": _group(dcu), "max": 1}
    assert res.status.value == "impossible"

    # A single DCU raises no group diagnostic.
    single = evaluate_fit(rifter, [_mod(dcu, SlotKind.LOW)],
                          skills=SkillProfile.from_dict({}))
    assert "max_group_fitted" not in _codes(single)


# --------------------------------------------------------------------------- #
# 7. rig_size_mismatch: medium rig on a frigate
# --------------------------------------------------------------------------- #
def test_medium_rig_on_frigate_impossible(rifter_ids):
    ids = rifter_ids
    rifter, rig = ids["Rifter"], ids["Medium Trimark Armor Pump I"]
    ship_size, rig_size = _attr(rifter, ATTR_RIG_SIZE), _attr(rig, ATTR_RIG_SIZE)
    assert (ship_size, rig_size) == (1, 2)                           # frigate vs medium
    res = evaluate_fit(rifter, [_mod(rig, SlotKind.RIG)],
                       skills=SkillProfile.from_dict({}))
    d = _diag(res, "rig_size_mismatch")
    assert d.params["rig_size"] == rig_size
    assert d.params["ship_rig_size"] == ship_size
    assert res.status.value == "impossible"


# --------------------------------------------------------------------------- #
# 8. Charge group + size validation
# --------------------------------------------------------------------------- #
def test_charge_group_and_size_validation(rifter_ids):
    ids = rifter_ids
    rifter, gun = ids["Rifter"], ids["150mm Light AutoCannon II"]
    none = SkillProfile.from_dict({})
    accepted = {int(_attr(gun, a)) for a in (604, 605)}              # chargeGroup1/2

    # EMP M: projectile ammo (group accepted) but chargeSize 2 vs the gun's 1.
    emp_m = ids["EMP M"]
    assert _group(emp_m) in accepted
    assert _attr(emp_m, A.CHARGE_SIZE) != _attr(gun, A.CHARGE_SIZE)
    res = evaluate_fit(rifter, [_mod(gun, SlotKind.HIGH, charge=emp_m)], skills=none)
    d = _diag(res, "charge_size_mismatch")
    assert d.params["module_size"] == _attr(gun, A.CHARGE_SIZE)
    assert d.params["charge_size"] == _attr(emp_m, A.CHARGE_SIZE)
    assert res.status.value == "impossible"

    # Antimatter Charge S: right size, but hybrid-ammo group not accepted by the gun.
    anti = ids["Antimatter Charge S"]
    assert _group(anti) not in accepted
    assert _attr(anti, A.CHARGE_SIZE) == _attr(gun, A.CHARGE_SIZE)
    res2 = evaluate_fit(rifter, [_mod(gun, SlotKind.HIGH, charge=anti)], skills=none)
    d2 = _diag(res2, "incompatible_charge")
    assert d2.params == {"type_id": gun, "charge_type_id": anti}
    assert res2.status.value == "impossible"

    # Control: the matching faction charge raises no charge diagnostics.
    ok = evaluate_fit(rifter, [_mod(gun, SlotKind.HIGH, charge=ids["Republic Fleet EMP S"])],
                      skills=none)
    assert not ({"incompatible_charge", "charge_size_mismatch"} & _codes(ok))


# --------------------------------------------------------------------------- #
# 9. Drone bandwidth / bay diagnostics (Tristan + Hobgoblins)
# --------------------------------------------------------------------------- #
def test_drone_bandwidth_and_bay(tristan_ids):
    ids = tristan_ids
    tristan, hob = ids["Tristan"], ids["Hobgoblin I"]
    bw = _attr(tristan, A.DRONE_BANDWIDTH)                           # 25 Mbit/s
    bay = _attr(tristan, A.DRONE_CAPACITY)                           # 40 m3
    hob_bw = _attr(hob, ATTR_DRONE_BW_USED)                          # 5
    hob_vol = _attr(hob, ATTR_VOLUME)                                # 5

    def drones(n):
        return [ModuleInput(type_id=hob, slot=SlotKind.DRONE,
                            state=ModuleState.ACTIVE, quantity=n)]

    # Exactly at bandwidth: clean.
    fit5 = evaluate_fit(tristan, drones(5))                          # All V
    r = fit5.telemetry["resources"]
    assert r["drone_bandwidth"] == pytest.approx(bw)
    assert r["drone_bandwidth_used"] == pytest.approx(5 * hob_bw)
    assert r["drone_bay"] == pytest.approx(bay)
    assert r["drone_bay_used"] == pytest.approx(5 * hob_vol)
    assert not ({"drone_bandwidth_exceeded", "drone_bay_exceeded"} & _codes(fit5))
    assert fit5.status.value == "valid"

    # One over bandwidth (30 > 25): resource error + only 5 counted for DPS.
    fit6 = evaluate_fit(tristan, drones(6))
    d = _diag(fit6, "drone_bandwidth_exceeded")
    assert d.params["used"] == pytest.approx(6 * hob_bw)
    assert d.params["cap"] == pytest.approx(bw)
    over_bw = _diag(fit6, "drones_over_bandwidth")
    assert over_bw.params["counted"] == int(bw // hob_bw)            # 5 of 6
    assert over_bw.params["requested"] == 6
    assert fit6.status.value == "over_resources"

    # Bay overflow (45 m3 > 40 m3) is structural: impossible beats over_resources.
    fit9 = evaluate_fit(tristan, drones(9))
    d9 = _diag(fit9, "drone_bay_exceeded")
    assert d9.params["used"] == pytest.approx(9 * hob_vol)
    assert d9.params["cap"] == pytest.approx(bay)
    assert fit9.status.value == "impossible"


# --------------------------------------------------------------------------- #
# 10. Status ladder: valid -> warnings -> missing_skills -> over_resources ->
#     impossible precedence
# --------------------------------------------------------------------------- #
def test_status_ladder_precedence(rifter_ids):
    ids = rifter_ids
    rifter, gun = ids["Rifter"], ids["150mm Light AutoCannon II"]
    none = SkillProfile.from_dict({})

    # (a) Bare hull, All V: nothing to complain about.
    a = evaluate_fit(rifter, [])
    assert a.status.value == "valid"
    assert not a.diagnostics and not a.missing_skills

    # (b) All V, active gun with no ammo: advisory only -> warnings.
    gun_active = ModuleInput(type_id=gun, slot=SlotKind.HIGH, state=ModuleState.ACTIVE)
    b = evaluate_fit(rifter, [gun_active])
    assert "missing_ammo" in _codes(b)
    assert not b.missing_skills
    assert b.status.value == "warnings"

    # (c) Same fit, untrained pilot: missing_skills outranks the warning.
    c = evaluate_fit(rifter, [gun_active], skills=none)
    assert "missing_ammo" in _codes(c)
    assert c.missing_skills                                          # gun needs skills
    assert c.status.value == "missing_skills"

    # (d) Add CPU-hungry EWAR until over budget: over_resources outranks missing_skills.
    heavy = [gun_active] * 3 + [
        _mod(ids["Warp Disruptor II"], SlotKind.MED),
        _mod(ids["Multispectrum Shield Hardener II"], SlotKind.MED),
        _mod(ids["Remote Sensor Dampener II"], SlotKind.MED),
        _mod(ids["Gyrostabilizer II"], SlotKind.LOW)]
    cpu_used = 3 * _attr(gun, A.CPU_USAGE) + sum(_attr(ids[n], A.CPU_USAGE) for n in (
        "Warp Disruptor II", "Multispectrum Shield Hardener II",
        "Remote Sensor Dampener II", "Gyrostabilizer II"))
    assert cpu_used > _attr(rifter, A.CPU_OUTPUT)                    # 178 > 130
    d = evaluate_fit(rifter, heavy, skills=none)
    assert "cpu_exceeded" in _codes(d)
    assert d.missing_skills
    assert d.status.value == "over_resources"

    # (e) Add a 4th med module: structural error outranks everything.
    e = evaluate_fit(rifter, heavy + [_mod(ids["Medium Shield Extender II"],
                                           SlotKind.MED)], skills=none)
    assert "too_many_modules" in _codes(e)
    assert "cpu_exceeded" in _codes(e)
    assert e.missing_skills
    assert e.status.value == "impossible"


# --------------------------------------------------------------------------- #
# 11. Loki + subsystems: slot/hardpoint folding and resource adds
# --------------------------------------------------------------------------- #
LOKI_SUBS = ("Loki Core - Augmented Nuclear Reactor",
             "Loki Defensive - Adaptive Defense Node",
             "Loki Offensive - Projectile Scoping Array",
             "Loki Propulsion - Wake Limiter")


def _loki_fit(ids):
    return [_mod(ids[n], SlotKind.SUBSYSTEM) for n in LOKI_SUBS]


def test_loki_subsystem_slot_and_resource_folding(loki_ids):
    ids = loki_ids
    loki = ids["Loki"]
    subs = [ids[n] for n in LOKI_SUBS]
    res = evaluate_fit(loki, _loki_fit(ids), skills=SkillProfile.from_dict({}))
    r = res.telemetry["resources"]

    def fold(ship_attr, sub_attr):
        base = _attr(loki, ship_attr)
        adds = 0.0
        for s in subs:
            from apps.sde.models import SdeTypeAttribute
            row = SdeTypeAttribute.objects.filter(type_id=s, attribute_id=sub_attr).first()
            adds += row.value if row else 0.0
        return base, adds

    # Slot folding: the hull has 0 racked slots; subsystems add them (attrs 1374-1376).
    for slot, ship_attr, sub_attr in (("high", A.HI_SLOTS, ATTR_SUB_HI),
                                      ("med", A.MED_SLOTS, ATTR_SUB_MED),
                                      ("low", A.LOW_SLOTS, ATTR_SUB_LOW)):
        base, adds = fold(ship_attr, sub_attr)
        assert base == 0
        assert r["slots"]["hull"][slot] == int(adds)                 # 7 / 5 / 5
    assert r["slots"]["hull"]["rig"] == int(_attr(loki, A.RIG_SLOTS))

    # Hardpoints from the Offensive subsystem (attrs 1368/1369): 5 turret, 2 launcher.
    _, t_add = fold(A.TURRET_HARDPOINTS, ATTR_SUB_TURRET)
    _, l_add = fold(A.LAUNCHER_HARDPOINTS, ATTR_SUB_LAUNCHER)
    assert r["hardpoints"]["turret"]["total"] == int(t_add)
    assert r["hardpoints"]["launcher"]["total"] == int(l_add)

    # CPU/PG/drone adds arrive as modAdd dogma effects from the Offensive subsystem
    # (its own cpuOutput/powerOutput/droneBandwidth/droneCapacity attrs).
    base_cpu, cpu_add = fold(A.CPU_OUTPUT, A.CPU_OUTPUT)             # 300 + 30
    assert r["cpu"]["output"] == pytest.approx(base_cpu + cpu_add, rel=2e-3)
    # Powergrid: the Offensive subsystem modAdds its own powerOutput (300 MW, effect
    # 3782) and the Core "Augmented Nuclear Reactor" then postPercents ship PG by its
    # powerEngineeringOutputBonus (attr 313 = +20 %, effect 490); modAdd applies before
    # postPercent in the canonical dogma operator order: (550 + 300) * 1.20 = 1020 MW.
    base_pg, pg_add = fold(A.POWER_OUTPUT, A.POWER_OUTPUT)           # 550 + 300
    core_pg_pct = _attr(ids[LOKI_SUBS[0]], 313)                      # +20 %
    assert r["powergrid"]["output"] == pytest.approx(
        (base_pg + pg_add) * (1 + core_pg_pct / 100.0), rel=2e-3)
    _, bw_add = fold(A.DRONE_BANDWIDTH, A.DRONE_BANDWIDTH)
    assert r["drone_bandwidth"] == pytest.approx(_attr(loki, A.DRONE_BANDWIDTH) + bw_add)
    _, bay_add = fold(A.DRONE_CAPACITY, A.DRONE_CAPACITY)
    assert r["drone_bay"] == pytest.approx(_attr(loki, A.DRONE_CAPACITY) + bay_add)

    # Defence folding: Defensive subsystem adds flat shield capacity (its attr 263),
    # armor (attr 1159 armorHPBonusAdd) and structure (attr 2688) via modAdd effects;
    # the Core/Defensive nodes also add capacitor capacity (their attr 482).
    layers = res.telemetry["defence"]["layers"]
    _, shield_add = fold(A.SHIELD_HP, A.SHIELD_HP)
    assert layers["shield"]["hp"] == pytest.approx(
        _attr(loki, A.SHIELD_HP) + shield_add, rel=2e-3)             # 2500 + 300
    _, armor_add = fold(A.ARMOR_HP, ATTR_ARMOR_PLATE_BONUS)
    assert layers["armor"]["hp"] == pytest.approx(
        _attr(loki, A.ARMOR_HP) + armor_add, rel=2e-3)               # 2500 + 300
    _, hull_add = fold(A.HULL_HP, ATTR_STRUCTURE_ADD)
    assert layers["hull"]["hp"] == pytest.approx(
        _attr(loki, A.HULL_HP) + hull_add, rel=2e-3)                 # 1000 + 100
    _, cap_add = fold(ATTR_CAP_CAPACITY, ATTR_CAP_CAPACITY)
    assert res.telemetry["capacitor"]["capacity"] == pytest.approx(
        _attr(loki, ATTR_CAP_CAPACITY) + cap_add, rel=2e-3)          # 1300 + 350

    # No structural/resource diagnostics; only untrained subsystem skills remain.
    assert not res.diagnostics
    assert res.status.value == "missing_skills"


# --------------------------------------------------------------------------- #
# 12. Subsystem skill bonus at level 1 vs 5 (Minmatar Core Systems, cap recharge)
# --------------------------------------------------------------------------- #
def test_loki_core_subsystem_skill_levels(loki_ids):
    ids = loki_ids
    loki = ids["Loki"]
    core = ids["Loki Core - Augmented Nuclear Reactor"]
    # Subsystem dogma: attr 1446 = -5 (% capacitor recharge time per level of Minmatar
    # Core Systems), pre-multiplied by the trained level (skill effect 3843) and applied
    # postPercent to the ship's rechargeRate (attr 55) by subsystem effect 4264.
    pct = _attr(core, ATTR_SUB_CORE_BONUS)                           # -5
    base_tau_ms = _attr(loki, ATTR_CAP_RECHARGE)                     # 325000 ms
    cap_total = _attr(loki, ATTR_CAP_CAPACITY) + sum(
        _attr(ids[n], ATTR_CAP_CAPACITY) for n in LOKI_SUBS[:2])     # hull + core + def

    for level in (1, 5):
        res = evaluate_fit(loki, _loki_fit(ids),
                           skills=SkillProfile.from_dict({MINMATAR_CORE_SYSTEMS: level}))
        cap = res.telemetry["capacitor"]
        tau_s = base_tau_ms * (1 + level * pct / 100.0) / 1000.0     # 308.75 / 243.75 s
        assert cap["recharge_s"] == pytest.approx(tau_s, rel=2e-3)
        assert cap["capacity"] == pytest.approx(cap_total, rel=2e-3)
        # Corrected recharge model: peak = 2.5 * C / tau.
        assert cap["peak_recharge"] == pytest.approx(2.5 * cap_total / tau_s, rel=2e-3)


# --------------------------------------------------------------------------- #
# 13. Offline module frees CPU/PG but still occupies its slot
# --------------------------------------------------------------------------- #
def test_offline_module_frees_cpu_and_pg(rifter_ids):
    ids = rifter_ids
    rifter, gyro = ids["Rifter"], ids["Gyrostabilizer II"]
    none = SkillProfile.from_dict({})

    on = evaluate_fit(rifter, [_mod(gyro, SlotKind.LOW, ModuleState.ONLINE)], skills=none)
    off = evaluate_fit(rifter, [_mod(gyro, SlotKind.LOW, ModuleState.OFFLINE)], skills=none)

    assert on.telemetry["resources"]["cpu"]["used"] == pytest.approx(
        _attr(gyro, A.CPU_USAGE), rel=2e-3)                          # 30 tf
    assert on.telemetry["resources"]["powergrid"]["used"] == pytest.approx(
        _attr(gyro, A.POWER_USAGE), rel=2e-3)                        # 1 MW
    assert off.telemetry["resources"]["cpu"]["used"] == 0.0
    assert off.telemetry["resources"]["powergrid"]["used"] == 0.0
    # Offline modules still occupy their rack slot.
    assert on.telemetry["resources"]["slots"]["used"]["low"] == 1
    assert off.telemetry["resources"]["slots"]["used"]["low"] == 1
