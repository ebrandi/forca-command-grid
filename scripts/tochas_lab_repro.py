"""Capture engine outputs for known-suspicious mechanics (remediation audit evidence).

Run inside the web container:
    docker compose run --rm -T web python scripts/tochas_lab_repro.py

Prints a JSON document of engine telemetry for a set of isolation fits chosen to
exercise mechanics suspected wrong/absent. Read-only; touches no application state.
"""
import json
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from apps.fitting.engine.adapter import FittingEngine  # noqa: E402
from apps.fitting.engine.types import (  # noqa: E402
    FitInput,
    ModuleInput,
    ModuleState,
    OperatingProfile,
    SkillProfile,
    SlotKind,
)
from apps.sde.models import SdeType  # noqa: E402


def tid(name):
    row = SdeType.objects.filter(name__iexact=name).values_list("type_id", flat=True).first()
    if row is None:
        raise SystemExit(f"type not found: {name}")
    return row


def run(label, ship, modules, note=""):
    engine = FittingEngine()
    fit = FitInput(ship_type_id=tid(ship), modules=tuple(modules))
    res = engine.evaluate(fit, SkillProfile.omniscient(), OperatingProfile())
    t = res.telemetry
    return {
        "label": label, "note": note, "ship": ship,
        "status": res.status.value,
        "unsupported": res.unsupported,
        "diag": [d.code for d in res.diagnostics],
        "resources": t.get("resources"),
        "defence": t.get("defence"),
        "capacitor": t.get("capacitor"),
        "offence": t.get("offence"),
        "mobility": t.get("mobility"),
        "targeting": t.get("targeting"),
    }


def mod(name, slot, state=ModuleState.ACTIVE, charge=None, qty=1):
    return ModuleInput(type_id=tid(name), slot=slot,
                       state=state, charge_type_id=tid(charge) if charge else None,
                       quantity=qty)


cases = []

# A. Bare hull baseline (hand-checkable vs pyfa/EVE: Rifter).
cases.append(run("rifter_bare", "Rifter", [], "All-V bare hull baseline"))

# B. Plate mass: align time must worsen with a 200mm plate (massAddition ignored?).
cases.append(run("punisher_plate", "Punisher",
               [mod("800mm Steel Plates II", SlotKind.LOW, ModuleState.ONLINE)],
               "800mm plate adds 2.4M kg mass; align must rise vs bare"))
cases.append(run("punisher_bare", "Punisher", [], "compare align vs plate case"))

# C. Active tank: does telemetry report ANY repair rate?
cases.append(run("merlin_active_shield", "Merlin",
               [mod("Medium Shield Booster II", SlotKind.MED, ModuleState.ACTIVE)],
               "expect shield boost HP/s somewhere in defence"))

# D. Passive regen: Merlin bare — peak shield regen expected in defence.
# (covered by merlin case + rifter case)

# E. Drone bandwidth: Vexor 75 Mbit/s; 5 Ogre II = 125 Mbit/s → must flag/limit.
cases.append(run("vexor_ogres", "Vexor",
               [mod("Ogre II", SlotKind.DRONE, ModuleState.ACTIVE, qty=5)],
               "5 heavy drones over 75Mbit bandwidth; DPS must not count all 5"))

# F. Weapon rig: Small Projectile Burst Aerator I must raise Rifter AC DPS.
rifter_guns = [mod("150mm Light AutoCannon II", SlotKind.HIGH, charge="Republic Fleet EMP S")
               for _ in range(3)]
cases.append(run("rifter_guns", "Rifter", rifter_guns, "3x150mm AC II + RF EMP S baseline"))
cases.append(run("rifter_guns_rig", "Rifter",
               rifter_guns + [mod("Small Projectile Burst Aerator I", SlotKind.RIG,
                                  ModuleState.ONLINE)],
               "same + RoF rig; DPS must rise ~10%"))

# G. Ammo range effect: Barrage vs EMP changes optimal/falloff (is range even reported?).
cases.append(run("rifter_barrage", "Rifter",
               [mod("150mm Light AutoCannon II", SlotKind.HIGH, charge="Barrage S")],
               "falloff bonus ammo; optimal/falloff/tracking expected in offence"))

# H. Missile stats: Caracal HML — flight time / range / applied numbers.
cases.append(run("caracal_hml", "Caracal",
               [mod("Heavy Missile Launcher II", SlotKind.HIGH, charge="Scourge Heavy Missile")
                for _ in range(5)],
               "missile range/flight time expected; hull kinetic bonus applies"))

# I. Cap booster: does injection count in capacitor stability?
cases.append(run("stabber_capbooster", "Stabber",
               [mod("Medium Capacitor Booster II", SlotKind.MED, ModuleState.ACTIVE,
                    charge="Navy Cap Booster 800"),
                mod("10MN Afterburner II", SlotKind.MED, ModuleState.ACTIVE)],
               "cap booster injection should improve stability"))

# J. Armor rig drawback + trimark: velocity penalty & armor HP bonus.
cases.append(run("rifter_trimark", "Rifter",
               [mod("Small Trimark Armor Pump I", SlotKind.RIG, ModuleState.ONLINE)],
               "expect +armor HP AND -velocity drawback vs bare"))

# K. Mixed stacking: 3x Gyrostabilizer II on Rifter guns (stacking-penalised).
cases.append(run("rifter_3gyro", "Rifter",
               rifter_guns + [mod("Gyrostabilizer II", SlotKind.LOW, ModuleState.ONLINE)
                              for _ in range(3)],
               "hand-check: dmg x1.1, x1.1*0.869, x1.1*0.571 stacking chain"))

# L. Implant slot: engine claims to accept implants — does it change anything?
cases.append(run("rifter_implant", "Rifter",
               [mod("Inherent Implants 'Squire' Capacitor Management EM-805",
                    SlotKind.IMPLANT, ModuleState.ONLINE)],
               "+5% cap implant; compare capacitor.capacity vs bare"))

print(json.dumps(cases, indent=1, default=str))
