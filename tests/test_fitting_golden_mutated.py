"""Golden fits: WS-11 mutated (abyssal) modules (engine v2, real SDE slice).

A mutated module is modelled as a set of rolled attribute overrides on the fitted type
(ModuleInput.attr_overrides). The override REPLACES the provider's base value for that
attribute — and ADDS it when the base type carries none, which is exactly the abyssal case:
the fittable "Abyssal Gyrostabilizer" SdeType (49730) stores only structural attrs
(mass/volume/skill), so its damageMultiplier / speedMultiplier live entirely in the override.
Everything downstream (the LocationGroupModifier damage/RoF chains, the stacking penalty on
the target attribute, validations, telemetry) flows through the normal pass-3 machinery.

Every expectation is hand-derived from the fixture's own base attributes plus CCP operator
semantics — never read back from the engine. Two differential cases (inert vs override,
abyssal-typed vs regular-typed with the same roll) prove the merge point, not a magic number.

Mechanics proven
----------------
* Override on a module that HAS the attribute (Gyrostabilizer II 519, damageMultiplier 1.1):
  the override wins over the base and flows through the 2-gyro stacking chain (attr 64 is
  non-stackable → penalised).
* Override on the RoF attribute (speedMultiplier 204 → gun's speed 51, non-stackable):
  changes DPS by exactly the computed factor.
* An abyssal-typed module (49730, no combat attrs) WITHOUT overrides is inert and raises the
  ``mutated_attributes_unknown`` WARNING (fit still valid); WITH overrides it is silent and
  behaves identically to a regular gyro carrying the same rolled attributes.
* EFT export emits pyfa's mutation-block syntax and re-imports to identical overrides + an
  identical FitInput.hash (FORCA→FORCA round-trip is lossless).
* views._parse_items bounds overrides (≤32, integer ids, finite floats).

Fixture: tests/fixtures/fitting/mutated.json (Rifter + 150mm AC II + RF EMP S + Gyrostabilizer
II + Abyssal Gyrostabilizer).
"""
from __future__ import annotations

import json
import math

import pytest

from apps.fitting.engine import attributes as A
from apps.fitting.engine.types import (
    FitInput,
    ModuleInput,
    ModuleState,
    SkillProfile,
    SlotKind,
    _freeze_overrides,
)

from ._fitting_graph_utils import evaluate_fit, load_graph_fixture

pytestmark = pytest.mark.django_db

S1 = math.exp(-((1 / 2.67) ** 2))          # second-element effectiveness in a penalised chain


@pytest.fixture()
def ids():
    return load_graph_fixture("mutated")


def _attr(type_id, attr_id):
    from apps.sde.models import SdeTypeAttribute
    return SdeTypeAttribute.objects.get(type_id=type_id, attribute_id=attr_id).value


def _has(type_id, attr_id) -> bool:
    from apps.sde.models import SdeTypeAttribute
    return SdeTypeAttribute.objects.filter(type_id=type_id, attribute_id=attr_id).exists()


def _shot_damage(ammo):
    return sum(_attr(ammo, a) for a in
               (A.EM_DAMAGE, A.THERMAL_DAMAGE, A.KINETIC_DAMAGE, A.EXPLOSIVE_DAMAGE)
               if _has(ammo, a))


def _penalised(base, deltas):
    """Apply a penalised multiplicative chain the way graph._calculate does (strongest first,
    factor S1**(i*i)); used only to reproduce CCP maths in-test, never to read the engine."""
    v = base
    for i, d in enumerate(sorted(deltas, key=abs, reverse=True)):
        v *= 1 + d * (S1 ** (i * i))
    return v


# --------------------------------------------------------------------------- #
# (a) override on damageMultiplier flows through the 2-gyro stacking chain
# --------------------------------------------------------------------------- #
def test_override_damage_multiplier_flows_through_stacking(ids):
    """3x 150mm AC II + RF EMP S + two Gyrostabilizer II (one base 1.1, one mutated to 1.35),
    NO skills — module-only and fully hand-computable. The mutated gyro's overridden
    damageMultiplier (attr 64, non-stackable) shares the penalised chain with the base gyro."""
    rifter, gun, ammo = ids["Rifter"], ids["150mm Light AutoCannon II"], ids["Republic Fleet EMP S"]
    gyro = ids["Gyrostabilizer II"]
    x_dmg = 1.35                                       # a "good roll" > the base 1.1
    mods = [ModuleInput(type_id=gun, slot=SlotKind.HIGH, charge_type_id=ammo) for _ in range(3)]
    mods += [ModuleInput(type_id=gyro, slot=SlotKind.LOW, state=ModuleState.ONLINE)]
    mods += [ModuleInput(type_id=gyro, slot=SlotKind.LOW, state=ModuleState.ONLINE,
                         attr_overrides={A.DAMAGE_MULTIPLIER: x_dmg})]
    res = evaluate_fit(rifter, mods, skills=SkillProfile.from_dict({}))

    shot = _shot_damage(ammo)
    base_mult = _attr(gun, A.DAMAGE_MULTIPLIER)
    d_base = _attr(gyro, A.DAMAGE_MULTIPLIER) - 1.0    # +10%
    d_mut = x_dmg - 1.0                                # +35% (override wins over base 1.1)
    dmg_mult = _penalised(base_mult, [d_base, d_mut])
    # RoF: both gyros keep the base speedMultiplier 0.895 (only 64 was overridden); gun speed
    # (attr 51) is non-stackable → the two -10.5% deltas penalise together.
    s = _attr(gyro, A.ROF_MULTIPLIER) - 1.0            # -0.105
    rof_ms = _penalised(_attr(gun, A.RATE_OF_FIRE), [s, s])
    expected = 3 * (shot * dmg_mult) / (rof_ms / 1000.0)
    assert res.telemetry["offence"]["total_dps"] == pytest.approx(expected, rel=2e-3)


# --------------------------------------------------------------------------- #
# (b) override on the RoF attribute changes DPS by exactly the computed factor
# --------------------------------------------------------------------------- #
def test_override_rof_attribute_changes_dps(ids):
    """3x 150mm AC II + RF EMP S + one Gyrostabilizer II mutated to speedMultiplier 0.8 (faster
    than the base 0.895). A single gyro → no stacking partner; damage uses the base 64=1.1."""
    rifter, gun, ammo = ids["Rifter"], ids["150mm Light AutoCannon II"], ids["Republic Fleet EMP S"]
    gyro = ids["Gyrostabilizer II"]
    y_rof = 0.8
    mods = [ModuleInput(type_id=gun, slot=SlotKind.HIGH, charge_type_id=ammo) for _ in range(3)]
    mods += [ModuleInput(type_id=gyro, slot=SlotKind.LOW, state=ModuleState.ONLINE,
                         attr_overrides={A.ROF_MULTIPLIER: y_rof})]
    res = evaluate_fit(rifter, mods, skills=SkillProfile.from_dict({}))

    shot = _shot_damage(ammo)
    dmg_mult = _attr(gun, A.DAMAGE_MULTIPLIER) * (1 + (_attr(gyro, A.DAMAGE_MULTIPLIER) - 1.0))
    rof_ms = _attr(gun, A.RATE_OF_FIRE) * (1 + (y_rof - 1.0))   # single, penalty factor ^0 = 1
    expected = 3 * (shot * dmg_mult) / (rof_ms / 1000.0)
    assert res.telemetry["offence"]["total_dps"] == pytest.approx(expected, rel=2e-3)

    # And the override genuinely moved the number: the same gyro at its base roll is slower.
    base = evaluate_fit(
        rifter,
        [ModuleInput(type_id=gun, slot=SlotKind.HIGH, charge_type_id=ammo) for _ in range(3)]
        + [ModuleInput(type_id=gyro, slot=SlotKind.LOW, state=ModuleState.ONLINE)],
        skills=SkillProfile.from_dict({}))
    assert res.telemetry["offence"]["total_dps"] > base.telemetry["offence"]["total_dps"]


# --------------------------------------------------------------------------- #
# (c) abyssal type WITHOUT overrides → warning, inert, fit valid
# --------------------------------------------------------------------------- #
def test_abyssal_without_overrides_warns_and_is_inert(ids):
    """An Abyssal Gyrostabilizer (49730 — no combat attrs) fitted with no overrides raises
    ``mutated_attributes_unknown`` (WARNING, non-structural) and changes nothing: its
    damageMultiplier / speedMultiplier evaluate to the dogma default 1.0."""
    rifter, gun, ammo = ids["Rifter"], ids["150mm Light AutoCannon II"], ids["Republic Fleet EMP S"]
    abyssal = ids["Abyssal Gyrostabilizer"]
    guns = [ModuleInput(type_id=gun, slot=SlotKind.HIGH, charge_type_id=ammo) for _ in range(3)]

    res = evaluate_fit(rifter, guns + [ModuleInput(type_id=abyssal, slot=SlotKind.LOW,
                                                   state=ModuleState.ONLINE)])
    codes = {(d.code, d.severity.value) for d in res.diagnostics}
    assert ("mutated_attributes_unknown", "warning") in codes
    assert res.status.value in ("valid", "warnings")

    # Inert: identical DPS to the same guns with no gyro at all.
    bare = evaluate_fit(rifter, guns)
    assert res.telemetry["offence"]["total_dps"] == pytest.approx(
        bare.telemetry["offence"]["total_dps"], rel=1e-9)


# --------------------------------------------------------------------------- #
# (d) abyssal type WITH overrides → no warning, behaves like the regular gyro
# --------------------------------------------------------------------------- #
def test_abyssal_with_overrides_no_warning_and_applies(ids):
    """An Abyssal Gyrostabilizer with rolled overrides {64: 1.4, 204: 0.75} does NOT warn and
    produces exactly the numbers of a regular Gyrostabilizer II carrying the same rolled attrs
    — proving the merge is type-agnostic (override adds the attr the abyssal base lacks)."""
    rifter, gun, ammo = ids["Rifter"], ids["150mm Light AutoCannon II"], ids["Republic Fleet EMP S"]
    abyssal, gyro = ids["Abyssal Gyrostabilizer"], ids["Gyrostabilizer II"]
    roll = {A.DAMAGE_MULTIPLIER: 1.4, A.ROF_MULTIPLIER: 0.75}
    guns = [ModuleInput(type_id=gun, slot=SlotKind.HIGH, charge_type_id=ammo) for _ in range(3)]

    ab = evaluate_fit(rifter, guns + [ModuleInput(type_id=abyssal, slot=SlotKind.LOW,
                                                  state=ModuleState.ONLINE, attr_overrides=roll)])
    reg = evaluate_fit(rifter, guns + [ModuleInput(type_id=gyro, slot=SlotKind.LOW,
                                                   state=ModuleState.ONLINE, attr_overrides=roll)])
    inert = evaluate_fit(rifter, guns + [ModuleInput(type_id=abyssal, slot=SlotKind.LOW,
                                                     state=ModuleState.ONLINE)])

    assert not any(d.code == "mutated_attributes_unknown" for d in ab.diagnostics)
    assert ab.telemetry["offence"]["total_dps"] == pytest.approx(
        reg.telemetry["offence"]["total_dps"], rel=1e-9)
    # The override actually did something (vs the inert abyssal gyro).
    assert ab.telemetry["offence"]["total_dps"] > inert.telemetry["offence"]["total_dps"]


# --------------------------------------------------------------------------- #
# (e) EFT export/import round-trips the mutation block exactly
# --------------------------------------------------------------------------- #
def test_eft_mutation_block_roundtrip(ids):
    from apps.fitting import services

    rifter, gun, ammo = ids["Rifter"], ids["150mm Light AutoCannon II"], ids["Republic Fleet EMP S"]
    gyro = ids["Gyrostabilizer II"]
    # Constructed in EFT export order (low before high) so the round-tripped module order — and
    # therefore FitInput.hash — matches the original exactly.
    original = [
        {"type_id": gyro, "slot": "low", "state": "active", "charge_type_id": None,
         "quantity": 1, "attr_overrides": {"64": 1.35, "204": 0.8}},
        {"type_id": gun, "slot": "high", "state": "active", "charge_type_id": ammo, "quantity": 1},
    ]
    eft = services.export_eft(rifter, original, "MutTest")

    # Exact pyfa block syntax: "[N] base", two-space mutaplasmid placeholder, sorted attr line.
    expected_block = ("[1] Gyrostabilizer II\n"
                      "  Unknown Mutaplasmid\n"
                      "  damageMultiplier 1.35, speedMultiplier 0.8")
    assert expected_block in eft
    assert "Gyrostabilizer II [1]" in eft            # rack line carries the reference

    parsed = services.import_eft(eft)
    assert parsed["ship_type_id"] == rifter
    gyro_item = next(it for it in parsed["items"] if it["type_id"] == gyro)
    assert gyro_item.get("attr_overrides") == {"64": 1.35, "204": 0.8}
    assert _freeze_overrides(gyro_item["attr_overrides"]) == ((64, 1.35), (204, 0.8))

    fit_before = services.fit_input_from_items(rifter, original)
    fit_after = services.fit_input_from_items(parsed["ship_type_id"], parsed["items"])
    assert fit_before.hash() == fit_after.hash()
    # Idempotent: re-exporting the imported fit yields byte-identical EFT.
    assert services.export_eft(parsed["ship_type_id"], parsed["items"], "MutTest") == eft


def test_eft_unresolved_attribute_name_surfaced(ids):
    """A mutation block naming an attribute FORCA doesn't know surfaces in ``unresolved`` (like
    an unresolved module name) and never crashes the import."""
    from apps.fitting import services

    eft = ("[Rifter, Bad]\n\n"
           "Gyrostabilizer II [1]\n\n"
           "[1] Gyrostabilizer II\n"
           "  Unknown Mutaplasmid\n"
           "  damageMultiplier 1.2, notARealAttribute 9")
    parsed = services.import_eft(eft)
    assert "notARealAttribute" in parsed["unresolved"]
    gyro_item = next(it for it in parsed["items"] if it["type_id"] == ids["Gyrostabilizer II"])
    assert gyro_item.get("attr_overrides") == {"64": 1.2}   # the resolvable one still lands


# --------------------------------------------------------------------------- #
# (f) _parse_items bounds the overrides payload
# --------------------------------------------------------------------------- #
def test_parse_items_bounds_overrides():
    from apps.fitting.views import _MAX_OVERRIDES, _parse_items

    # >cap entries → truncated to _MAX_OVERRIDES; ids stay str(int), values finite floats.
    big = {str(1000 + i): float(i) for i in range(_MAX_OVERRIDES + 8)}
    out = _parse_items(json.dumps([{"type_id": 519, "slot": "low", "attr_overrides": big}]))
    ov = out[0]["attr_overrides"]
    assert len(ov) == _MAX_OVERRIDES
    assert all(isinstance(v, float) and math.isfinite(v) for v in ov.values())
    assert all(int(k) or k == "0" for k in ov)

    # Non-numeric value and non-integer key are dropped, not fatal; the good pair survives.
    out2 = _parse_items(json.dumps([{"type_id": 519, "slot": "low",
                                     "attr_overrides": {"64": 1.4, "999": "x", "bad": 2.0}}]))
    assert out2[0]["attr_overrides"] == {"64": 1.4}

    # Non-finite values are rejected.
    out3 = _parse_items(json.dumps([{"type_id": 519, "slot": "low",
                                     "attr_overrides": {"64": float("inf"), "204": 0.9}}]))
    assert out3[0]["attr_overrides"] == {"204": 0.9}

    # A module with no overrides carries no attr_overrides key (unchanged hash for old fits).
    out4 = _parse_items(json.dumps([{"type_id": 519, "slot": "low"}]))
    assert "attr_overrides" not in out4[0]


def test_module_input_hash_folds_overrides():
    """A mutation makes a distinct FitInput (WS-13 save-dedup sees the change), and an ordinary
    module's hash is unchanged from before WS-11 (no attr_overrides key in canonical)."""
    plain = ModuleInput(type_id=519, slot=SlotKind.LOW)
    mutated = ModuleInput(type_id=519, slot=SlotKind.LOW, attr_overrides={64: 1.3})
    assert "attr_overrides" not in plain.canonical()
    assert FitInput(ship_type_id=587, modules=(plain,)).hash() \
        != FitInput(ship_type_id=587, modules=(mutated,)).hash()
    # Order/key-type independence: dict vs sorted pairs vs str keys hash the same.
    a = ModuleInput(type_id=519, slot=SlotKind.LOW, attr_overrides={204: 0.8, 64: 1.3})
    b = ModuleInput(type_id=519, slot=SlotKind.LOW, attr_overrides=[(64, 1.3), (204, 0.8)])
    assert a.attr_overrides == b.attr_overrides == ((64, 1.3), (204, 0.8))
