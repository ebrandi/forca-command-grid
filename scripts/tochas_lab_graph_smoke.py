"""Smoke-test the generic graph evaluator (passes 1-3) against the live SDE import.

Hand-computed expectations only — nothing read back from the implementation.
    docker compose run --rm -T web python scripts/tochas_lab_graph_smoke.py
"""
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from apps.fitting.engine import attributes as A  # noqa: E402
from apps.fitting.engine.adapter import ORMDataProvider  # noqa: E402
from apps.fitting.engine.graph import evaluate_attributes  # noqa: E402
from apps.fitting.engine.types import (  # noqa: E402
    FitInput, ModuleInput, ModuleState, SkillProfile, SlotKind,
)
from apps.sde.models import SdeType  # noqa: E402


def tid(name):
    v = SdeType.objects.filter(name__iexact=name).values_list("type_id", flat=True).first()
    assert v, f"missing type {name}"
    return v


def build(ship, modules, skills=None):
    prov = ORMDataProvider()
    fit = FitInput(ship_type_id=tid(ship), modules=tuple(modules))
    sk = skills or SkillProfile.omniscient()
    return evaluate_attributes(fit, sk, prov, skill_ids=prov.trained_skill_ids()), prov


def mod(name, slot, state=ModuleState.ACTIVE, charge=None):
    return ModuleInput(type_id=tid(name), slot=slot, state=state,
                       charge_type_id=tid(charge) if charge else None)


def check(label, got, want, tol=0.005):
    ok = abs(got - want) <= tol * max(1.0, abs(want))
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got {got:.4f} want {want:.4f}")
    return ok


fails = 0

# 1. Plate mass: Punisher base mass 1_282_000 (whatever base is) + 800mm II adds via modAdd.
ev, prov = build("Punisher", [mod("800mm Steel Plates II", SlotKind.LOW, ModuleState.ONLINE)],
                 SkillProfile.from_dict({}))
base_mass = prov.attrs(tid("Punisher")).get(A.MASS, 0.0)
plate_add = prov.attrs(tid("800mm Steel Plates II")).get(796, 0.0)
fails += not check("punisher+800mm mass", ev.ship_value(A.MASS), base_mass + plate_add)

# 2. Trimark: +armor HP % and -velocity drawback, untrained (isolate the rig).
ev, prov = build("Rifter", [mod("Small Trimark Armor Pump I", SlotKind.RIG, ModuleState.ONLINE)],
                 SkillProfile.from_dict({}))
rift = prov.attrs(tid("Rifter"))
tri = prov.attrs(tid("Small Trimark Armor Pump I"))
armor_pct = tri.get(335, 0.0)      # armorHpBonus (%)
drawback = tri.get(1138, 0.0)      # drawback (% velocity)
fails += not check("rifter+trimark armorHP", ev.ship_value(A.ARMOR_HP),
                   rift[A.ARMOR_HP] * (1 + armor_pct / 100.0))
# CCP data: effect 2717 drawbackAgility postPercents AGILITY (attr 70) by attr 1138 (+10).
fails += not check("rifter+trimark agility", ev.ship_value(A.AGILITY),
                   rift[A.AGILITY] * (1 + drawback / 100.0))
fails += not check("rifter+trimark velocity unchanged", ev.ship_value(A.MAX_VELOCITY),
                   rift[A.MAX_VELOCITY])

# 3. Stacking: 3x Gyrostabilizer II on the gun's damageMultiplier, untrained.
ev, prov = build("Rifter",
                 [mod("150mm Light AutoCannon II", SlotKind.HIGH),
                  *[mod("Gyrostabilizer II", SlotKind.LOW, ModuleState.ONLINE)] * 3],
                 SkillProfile.from_dict({}))
gun_entity = next(e for e in ev.modules if e.type_id == tid("150mm Light AutoCannon II"))
gyro = prov.attrs(tid("Gyrostabilizer II"))
gun = prov.attrs(tid("150mm Light AutoCannon II"))
gv = gyro[A.DAMAGE_MULTIPLIER] - 1.0           # e.g. +0.10
S = [1.0, 0.8691199808003974, 0.5705831435105683]
want = gun[A.DAMAGE_MULTIPLIER]
for i in range(3):
    want *= 1 + gv * S[i]
fails += not check("3xGyroII stacking dmgMult", ev.value(gun_entity, A.DAMAGE_MULTIPLIER), want)

# 4. Per-level skill scaling: Surgical Strike at III → +9% turret damage (3%/lvl).
ev, prov = build("Rifter", [mod("150mm Light AutoCannon II", SlotKind.HIGH)],
                 SkillProfile.from_dict({3315: 3, 3302: 0}))
gun_entity = next(e for e in ev.modules if e.type_id == tid("150mm Light AutoCannon II"))
fails += not check("SurgicalStrike III dmgMult", ev.value(gun_entity, A.DAMAGE_MULTIPLIER),
                   gun[A.DAMAGE_MULTIPLIER] * 1.09)

# 5. Hull trait per-level: Rifter trait = -7.5%/lvl Small Projectile RATE OF FIRE
# (ship attr 460, pre-multiplied by the Minmatar Frigate skill's level via effect 453,
# applied to the gun's speed attr 51 via LocationRequiredSkillModifier on 3302).
ev, prov = build("Rifter", [mod("150mm Light AutoCannon II", SlotKind.HIGH)],
                 SkillProfile.from_dict({3329: 4}))  # Minmatar Frigate IV
gun_entity = next(e for e in ev.modules if e.type_id == tid("150mm Light AutoCannon II"))
fails += not check("Rifter RoF trait MF IV", ev.value(gun_entity, A.RATE_OF_FIRE),
                   gun[A.RATE_OF_FIRE] * (1 - 0.075 * 4))

# 6. Implant: 'Squire' EM-805 (+5% cap) — capacitor capacity rises.
ev, prov = build("Rifter",
                 [mod("Inherent Implants 'Squire' Capacitor Management EM-805",
                      SlotKind.IMPLANT)], SkillProfile.from_dict({}))
fails += not check("EM-805 implant cap", ev.ship_value(A.CAP_CAPACITY),
                   prov.attrs(tid("Rifter"))[A.CAP_CAPACITY] * 1.05)

# 7. Shield extender: modAdd shield HP + flat sig add, untrained.
ev, prov = build("Rifter",
                 [mod("Medium Shield Extender II", SlotKind.MED, ModuleState.ONLINE)],
                 SkillProfile.from_dict({}))
ext = prov.attrs(tid("Medium Shield Extender II"))
fails += not check("MSE-II shield HP", ev.ship_value(A.SHIELD_HP),
                   rift[A.SHIELD_HP] + ext[72])
fails += not check("MSE-II sig", ev.ship_value(A.SIGNATURE_RADIUS),
                   rift[A.SIGNATURE_RADIUS] + ext[A.SIG_RADIUS_ADD])

# 8. Offline module applies nothing.
ev, prov = build("Rifter",
                 [mod("Medium Shield Extender II", SlotKind.MED, ModuleState.OFFLINE)],
                 SkillProfile.from_dict({}))
fails += not check("offline MSE-II inert", ev.ship_value(A.SHIELD_HP), rift[A.SHIELD_HP])

print("\ndiagnostics sample:", ev.diagnostics[:5])
print("FAILURES:", fails)
raise SystemExit(1 if fails else 0)
