"""Golden fits: WS-3 fitting-legality validation (engine v2, real SDE slices).

Covers the validations added in WS-3, each proven against a real CCP data slice with
hand-derived expectations (never read back from the engine):

* ``max_group_active_exceeded`` — more modules of a group ACTIVE than maxGroupActive(763)
  allows (propulsion modules carry maxGroupActive=1).
* ``max_group_online_exceeded`` — more modules of a group ONLINE than maxGroupOnline(978)
  allows (Mining Survey Chipset / Command Bursts carry maxGroupOnline=1); isolated from
  the active cap because the chipset carries 978 but not 763.
* ``ship_restriction_violated`` — a module whose canFitShipGroup*/canFitShipType*/
  fitsToShipType whitelist excludes the hull (Siege Module II fits only Dreadnought /
  Lancer Dreadnought groups).
* ``implant_slot_conflict`` / ``booster_slot_conflict`` — two implants sharing an
  implantness(331) slot, or two boosters sharing a boosterness(1087) slot.
* ``subsystem_slot_conflict`` / ``subsystem_count_invalid`` — two subsystems in the same
  subSystemSlot(1366), or a Strategic Cruiser without one subsystem in every slot. The
  required count is the 4 distinct subsystem slots the hull's subsystems expose (via
  fitsToShipType==hull), NOT the stale maxSubSystems(1367)=5 hull attribute.

Fixtures: tests/fixtures/fitting/fitval_restrictions.json (this file's cases) and the
existing fitval_loki.json (subsystems). Every id/value is asserted from the slice's own
SDE rows before it is used, so a fixture drift fails loudly rather than silently passing.
"""
from __future__ import annotations

import pytest

from apps.fitting.engine import attributes as A
from apps.fitting.engine.types import ModuleInput, ModuleState, SkillProfile, SlotKind

from ._fitting_graph_utils import evaluate_fit, load_graph_fixture

pytestmark = pytest.mark.django_db


@pytest.fixture()
def rst_ids():
    return load_graph_fixture("fitval_restrictions")


@pytest.fixture()
def loki_ids():
    return load_graph_fixture("fitval_loki")


def _attr(type_id, attr_id):
    from apps.sde.models import SdeTypeAttribute
    return SdeTypeAttribute.objects.get(type_id=type_id, attribute_id=attr_id).value


def _attr_opt(type_id, attr_id):
    from apps.sde.models import SdeTypeAttribute
    row = SdeTypeAttribute.objects.filter(type_id=type_id, attribute_id=attr_id).first()
    return row.value if row else None


def _group(type_id):
    from apps.sde.models import SdeType
    return SdeType.objects.get(type_id=type_id).group_id


def _codes(res):
    return {d.code for d in res.diagnostics}


def _diag(res, code):
    hits = [d for d in res.diagnostics if d.code == code]
    assert hits, f"expected diagnostic {code}, got {sorted(_codes(res))}"
    return hits[0]


def _mod(tid, slot, state=ModuleState.ACTIVE, charge=None, qty=1):
    return ModuleInput(type_id=tid, slot=slot, state=state, charge_type_id=charge,
                       quantity=qty)


# --------------------------------------------------------------------------- #
# 1. maxGroupActive: two active propulsion modules on one hull
# --------------------------------------------------------------------------- #
def test_max_group_active_two_afterburners(rst_ids):
    ids = rst_ids
    rifter, ab = ids["Rifter"], ids["1MN Afterburner II"]
    # Data: an afterburner is a Propulsion Module (group 46) capped at one active.
    assert _attr(ab, A.MAX_GROUP_ACTIVE) == 1
    assert _attr_opt(ab, A.MAX_GROUP_ONLINE) is None            # no online cap on prop mods

    both_active = evaluate_fit(rifter, [_mod(ab, SlotKind.MED, ModuleState.ACTIVE),
                                        _mod(ab, SlotKind.MED, ModuleState.ACTIVE)])
    d = _diag(both_active, "max_group_active_exceeded")
    assert d.params == {"group_id": _group(ab), "max": 1}
    assert both_active.status.value == "impossible"

    # One active + one merely online: active count is 1 ≤ 1, so no violation.
    mixed = evaluate_fit(rifter, [_mod(ab, SlotKind.MED, ModuleState.ACTIVE),
                                  _mod(ab, SlotKind.MED, ModuleState.ONLINE)])
    assert "max_group_active_exceeded" not in _codes(mixed)
    # A single active afterburner is fine.
    single = evaluate_fit(rifter, [_mod(ab, SlotKind.MED, ModuleState.ACTIVE)])
    assert "max_group_active_exceeded" not in _codes(single)


# --------------------------------------------------------------------------- #
# 2. maxGroupOnline: two online modules of an online-capped group (isolated from active)
# --------------------------------------------------------------------------- #
def test_max_group_online_two_mining_chipsets(rst_ids):
    ids = rst_ids
    rifter, chip = ids["Rifter"], ids["Mining Survey Chipset I"]
    # Data: Mining Survey Chipset carries maxGroupOnline=1 but NO maxGroupActive — so it
    # exercises the online cap alone.
    assert _attr(chip, A.MAX_GROUP_ONLINE) == 1
    assert _attr_opt(chip, A.MAX_GROUP_ACTIVE) is None

    two_online = evaluate_fit(rifter, [_mod(chip, SlotKind.MED, ModuleState.ONLINE),
                                       _mod(chip, SlotKind.MED, ModuleState.ONLINE)])
    d = _diag(two_online, "max_group_online_exceeded")
    assert d.params == {"group_id": _group(chip), "max": 1}
    # The active cap must NOT fire — the modules are only online, and carry no 763 anyway.
    assert "max_group_active_exceeded" not in _codes(two_online)
    assert two_online.status.value == "impossible"

    single = evaluate_fit(rifter, [_mod(chip, SlotKind.MED, ModuleState.ONLINE)])
    assert "max_group_online_exceeded" not in _codes(single)


# --------------------------------------------------------------------------- #
# 3. ship restriction: Siege Module II only fits Dreadnought-class hulls
# --------------------------------------------------------------------------- #
def test_ship_restriction_siege_module_on_wrong_hull(rst_ids):
    ids = rst_ids
    siege = ids["Siege Module II"]
    rifter, rev = ids["Rifter"], ids["Revelation"]
    # Data: Siege Module II whitelists hull GROUPS 485 (Dreadnought) and 4594 (Lancer
    # Dreadnought); a Rifter is group 25 (Frigate), a Revelation is group 485.
    allowed = {int(_attr(siege, A.CAN_FIT_SHIP_GROUP_ATTRS[0])),
               int(_attr(siege, A.CAN_FIT_SHIP_GROUP_ATTRS[1]))}
    assert allowed == {485, 4594}
    assert _group(rifter) not in allowed and _group(rev) in allowed

    bad = evaluate_fit(rifter, [_mod(siege, SlotKind.HIGH, ModuleState.OFFLINE)])
    d = _diag(bad, "ship_restriction_violated")
    assert d.params["type_id"] == siege
    assert d.params["allowed_groups"] == [485, 4594]
    assert d.params["ship_group_id"] == _group(rifter)
    assert bad.status.value == "impossible"

    # The same module on a Dreadnought raises no restriction diagnostic.
    ok = evaluate_fit(rev, [_mod(siege, SlotKind.HIGH, ModuleState.OFFLINE)])
    assert "ship_restriction_violated" not in _codes(ok)


# --------------------------------------------------------------------------- #
# 4. implant slot conflict (two implants sharing implantness)
# --------------------------------------------------------------------------- #
def test_implant_slot_conflict(rst_ids):
    ids = rst_ids
    basic, std = ids["Memory Augmentation - Basic"], ids["Memory Augmentation - Standard"]
    other = ids["Ocular Filter - Basic"]
    # Data: both Memory Augmentations sit in implant slot 2; the Ocular Filter is slot 1.
    assert _attr(basic, A.IMPLANTNESS) == _attr(std, A.IMPLANTNESS) == 2
    assert _attr(other, A.IMPLANTNESS) == 1

    clash = evaluate_fit(ids["Rifter"], [_mod(basic, SlotKind.IMPLANT),
                                         _mod(std, SlotKind.IMPLANT)])
    d = _diag(clash, "implant_slot_conflict")
    assert d.params["slot"] == 2
    assert clash.status.value == "impossible"

    # Different slots coexist cleanly.
    ok = evaluate_fit(ids["Rifter"], [_mod(basic, SlotKind.IMPLANT),
                                      _mod(other, SlotKind.IMPLANT)])
    assert "implant_slot_conflict" not in _codes(ok)


# --------------------------------------------------------------------------- #
# 5. booster slot conflict (two boosters sharing boosterness)
# --------------------------------------------------------------------------- #
def test_booster_slot_conflict(rst_ids):
    ids = rst_ids
    std, imp = ids["Standard Blue Pill Booster"], ids["Improved Blue Pill Booster"]
    other = ids["Standard Sooth Sayer Booster"]
    # Data: both Blue Pill boosters sit in booster slot 1; the Sooth Sayer is slot 2.
    assert _attr(std, A.BOOSTERNESS) == _attr(imp, A.BOOSTERNESS) == 1
    assert _attr(other, A.BOOSTERNESS) == 2

    clash = evaluate_fit(ids["Rifter"], [_mod(std, SlotKind.BOOSTER),
                                         _mod(imp, SlotKind.BOOSTER)])
    d = _diag(clash, "booster_slot_conflict")
    assert d.params["slot"] == 1
    assert clash.status.value == "impossible"

    ok = evaluate_fit(ids["Rifter"], [_mod(std, SlotKind.BOOSTER),
                                      _mod(other, SlotKind.BOOSTER)])
    assert "booster_slot_conflict" not in _codes(ok)


# --------------------------------------------------------------------------- #
# 6+7. subsystems: same-slot conflict and incomplete-set count (Loki fixture)
# --------------------------------------------------------------------------- #
_CORE = "Loki Core - Augmented Nuclear Reactor"
_DEF = "Loki Defensive - Adaptive Defense Node"
_OFF = "Loki Offensive - Projectile Scoping Array"
_PROP = "Loki Propulsion - Wake Limiter"


def test_subsystem_slot_conflict(loki_ids):
    ids = loki_ids
    loki = ids["Loki"]
    core, off, prop = ids[_CORE], ids[_OFF], ids[_PROP]
    # Data: the four Loki subsystems occupy four distinct slots (125-128); two Cores would
    # both demand slot 125.
    assert _attr(core, A.SUBSYSTEM_SLOT) == 125
    assert {int(_attr(ids[n], A.SUBSYSTEM_SLOT)) for n in (_CORE, _DEF, _OFF, _PROP)} \
        == {125, 126, 127, 128}

    # Two Cores + Offensive + Propulsion: 4 subsystems (so the count is right) but slot 125
    # is doubled → slot conflict, and NOT a count error.
    res = evaluate_fit(loki, [_mod(core, SlotKind.SUBSYSTEM),
                              _mod(core, SlotKind.SUBSYSTEM),
                              _mod(off, SlotKind.SUBSYSTEM),
                              _mod(prop, SlotKind.SUBSYSTEM)],
                       skills=SkillProfile.from_dict({}))
    d = _diag(res, "subsystem_slot_conflict")
    assert d.params["slot"] == 125
    assert "subsystem_count_invalid" not in _codes(res)
    assert res.status.value == "impossible"


def test_subsystem_count_invalid(loki_ids):
    ids = loki_ids
    loki = ids["Loki"]
    subs4 = [ids[n] for n in (_CORE, _DEF, _OFF, _PROP)]
    # The hull marks itself a Strategic Cruiser via maxSubSystems, but that value is stale
    # (5); the true requirement is the 4 distinct subsystem slots its subsystems expose.
    assert _attr(loki, A.MAX_SUBSYSTEMS) == 5

    # Only three of the four subsystems fitted → incomplete, no slot clash.
    three = evaluate_fit(loki, [_mod(t, SlotKind.SUBSYSTEM) for t in subs4[:3]],
                         skills=SkillProfile.from_dict({}))
    d = _diag(three, "subsystem_count_invalid")
    assert d.params == {"fitted": 3, "required": 4}
    assert "subsystem_slot_conflict" not in _codes(three)
    assert three.status.value == "impossible"

    # The complete four-subsystem set raises neither subsystem diagnostic.
    full = evaluate_fit(loki, [_mod(t, SlotKind.SUBSYSTEM) for t in subs4],
                        skills=SkillProfile.from_dict({}))
    assert not ({"subsystem_count_invalid", "subsystem_slot_conflict"} & _codes(full))
