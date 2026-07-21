#!/usr/bin/env python3
"""Third-engine differential verification for Tocha's Lab (WS-15a / audit OPEN-2).

Evaluates a set of representative ship fits through BOTH engines and compares the key
stats within documented, justified tolerances:

  * the FORCA fitting engine (``apps.fitting.engine`` v2), reached read-only through the
    ``apps.fitting.services`` adapter inside the ``web`` container, and
  * pyfa's ``eos`` (headless, driven through its own venv against its own bundled SQLite
    SDE) as an INDEPENDENT reference implementation.

The harness exits non-zero if any compared stat is outside its tolerance and the deviation
is not explicitly explained (per-fit ``expect`` skip with a reason). It is a developer /
pre-merge gate, not a CI-mandatory step (pyfa must be set up locally first).

pyfa is GPL. This file RUNS pyfa as an external tool and drives its public ``eos`` API; it
contains NO pyfa code. The pyfa-side driver embedded below is our own orchestration code
that imports and calls pyfa — it does not reproduce pyfa's implementation.

--------------------------------------------------------------------------------
One-time local setup (dev machine only; see scratchpad NOTES.md for the full recipe)
--------------------------------------------------------------------------------
  1. Clone pyfa somewhere, e.g.  <PYFA_ROOT> = scratchpad/pyfa
  2. python3.13 -m venv <PYFA_VENV>            # 3.13; 3.14 is too new for SQLAlchemy 1.4.50
     <PYFA_VENV>/bin/pip install "sqlalchemy==1.4.50" "logbook==1.7.0.post0"
  3. Build pyfa's eve.db from its bundled staticdata (no wx needed) — the harness will do
     this automatically on first run, or run build_eve_db.py by hand.

Run:
    python scripts/tochas_lab_differential_pyfa.py
    python scripts/tochas_lab_differential_pyfa.py --fits rifter_ac,stabber_mwd --json
    python scripts/tochas_lab_differential_pyfa.py --pyfa-root /path/to/pyfa --pyfa-venv /path/to/venv

Exit codes: 0 = all stats within tolerance (or explained); 1 = unexplained deviation(s);
2 = setup / evaluation error (pyfa missing, container down, engine raised).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
from collections import Counter

# --------------------------------------------------------------------------- #
# Local-setup defaults. These point at the WS-15a scratchpad and MUST be set up
# by hand (they are intentionally outside the repo — pyfa is GPL and its SDE is
# large). The harness fails loudly with instructions if they are missing.
# --------------------------------------------------------------------------- #
_SCRATCH = ("/tmp/claude-1000/-home-ebrandi-projects-forca-command-grid/"
            "ade3d10a-23de-4818-ab00-bc662e12a6d4/scratchpad")
DEFAULT_PYFA_ROOT = os.environ.get("PYFA_ROOT", os.path.join(_SCRATCH, "pyfa"))
DEFAULT_PYFA_VENV = os.environ.get("PYFA_VENV", os.path.join(_SCRATCH, "pyfa-venv"))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------- #
# Fit catalogue.
#
# Each fit carries TWO parallel representations, tied together and cross-checked
# (``_consistency_guard``) so they can never silently drift:
#   * ``our_items`` — OUR canonical items-blob (the exact shape apps.fitting.services
#     .fit_input_from_items consumes): type NAME + slot token + module state. This drives
#     the FORCA engine. Passive damage/buffer mods carry state "online"; things that must be
#     switched on (weapons, prop, active hardeners/reps, webs) carry "active".
#   * ``eft`` — the EFT text a pyfa user would paste. This is what DEFINES the pyfa side:
#     the harness parses it (``_parse_eft``) and drives pyfa's eos with the parsed modules.
#     Module state on the pyfa side is derived identically (highest valid active state,
#     "/offline" honoured) so both engines see the same effective states.
#
# ``expect`` lets a fit document a KNOWN, JUSTIFIED per-stat deviation (model difference,
# not a bug) as {stat: "reason"}; that stat is then reported but not failed.
# --------------------------------------------------------------------------- #
FITS = [
    {
        "key": "rifter_ac",
        "name": "Rifter — 150mm AC (buffer-less brawler)",
        "ship": "Rifter",
        "damage_pattern": [25, 25, 25, 25],
        "our_items": [
            {"name": "150mm Light AutoCannon II", "slot": "high", "charge": "Republic Fleet EMP S", "state": "active"},
            {"name": "150mm Light AutoCannon II", "slot": "high", "charge": "Republic Fleet EMP S", "state": "active"},
            {"name": "150mm Light AutoCannon II", "slot": "high", "charge": "Republic Fleet EMP S", "state": "active"},
            {"name": "Gyrostabilizer II", "slot": "low", "state": "online"},
            {"name": "Gyrostabilizer II", "slot": "low", "state": "online"},
            {"name": "Small Projectile Burst Aerator I", "slot": "rig", "state": "online"},
        ],
        "eft": """
            [Rifter, Rifter — 150mm AC]
            Gyrostabilizer II
            Gyrostabilizer II

            150mm Light AutoCannon II, Republic Fleet EMP S
            150mm Light AutoCannon II, Republic Fleet EMP S
            150mm Light AutoCannon II, Republic Fleet EMP S

            Small Projectile Burst Aerator I
        """,
    },
    {
        "key": "caracal_hml",
        "name": "Caracal — Heavy Missile shield (MWD)",
        "ship": "Caracal",
        "damage_pattern": [25, 25, 25, 25],
        "our_items": [
            {"name": "Heavy Missile Launcher II", "slot": "high", "charge": "Scourge Fury Heavy Missile", "state": "active"},
            {"name": "Heavy Missile Launcher II", "slot": "high", "charge": "Scourge Fury Heavy Missile", "state": "active"},
            {"name": "Heavy Missile Launcher II", "slot": "high", "charge": "Scourge Fury Heavy Missile", "state": "active"},
            {"name": "Heavy Missile Launcher II", "slot": "high", "charge": "Scourge Fury Heavy Missile", "state": "active"},
            {"name": "Heavy Missile Launcher II", "slot": "high", "charge": "Scourge Fury Heavy Missile", "state": "active"},
            {"name": "50MN Y-T8 Compact Microwarpdrive", "slot": "med", "state": "active"},
            {"name": "Large Shield Extender II", "slot": "med", "state": "online"},
            {"name": "Large Shield Extender II", "slot": "med", "state": "online"},
            {"name": "Multispectrum Shield Hardener II", "slot": "med", "state": "active"},
            {"name": "EM Shield Amplifier II", "slot": "med", "state": "online"},
            {"name": "Ballistic Control System II", "slot": "low", "state": "online"},
            {"name": "Ballistic Control System II", "slot": "low", "state": "online"},
            {"name": "Ballistic Control System II", "slot": "low", "state": "online"},
            {"name": "Damage Control II", "slot": "low", "state": "online"},
            {"name": "Medium Core Defense Field Extender II", "slot": "rig", "state": "online"},
            {"name": "Medium Core Defense Field Extender II", "slot": "rig", "state": "online"},
            {"name": "Medium Bay Loading Accelerator II", "slot": "rig", "state": "online"},
        ],
        "eft": """
            [Caracal, Caracal — HML shield]
            Ballistic Control System II
            Ballistic Control System II
            Ballistic Control System II
            Damage Control II

            50MN Y-T8 Compact Microwarpdrive
            Large Shield Extender II
            Large Shield Extender II
            Multispectrum Shield Hardener II
            EM Shield Amplifier II

            Heavy Missile Launcher II, Scourge Fury Heavy Missile
            Heavy Missile Launcher II, Scourge Fury Heavy Missile
            Heavy Missile Launcher II, Scourge Fury Heavy Missile
            Heavy Missile Launcher II, Scourge Fury Heavy Missile
            Heavy Missile Launcher II, Scourge Fury Heavy Missile

            Medium Core Defense Field Extender II
            Medium Core Defense Field Extender II
            Medium Bay Loading Accelerator II
        """,
        # Previously carried an `expect` for sig_radius documenting a FORCA stacking bug: the
        # MWD's signatureRadiusBonus (+500%) was NOT placed in the same stacking-penalised
        # group as the two Core Defense Field Extender rig sig penalties (+5% each). Fixed
        # (WS-15 sigfix): the group (MWD, rig, rig) now yields x6.4394, so (125 base +50 flat
        # LSE) x6.4394 = 1126.9, matching pyfa. The comparison is STRICT again — no expect.
    },
    {
        "key": "omen_pulse",
        "name": "Omen — Heavy Pulse laser (active armor, AB)",
        "ship": "Omen",
        "damage_pattern": [25, 25, 25, 25],
        "our_items": [
            {"name": "Heavy Pulse Laser II", "slot": "high", "charge": "Imperial Navy Multifrequency M", "state": "active"},
            {"name": "Heavy Pulse Laser II", "slot": "high", "charge": "Imperial Navy Multifrequency M", "state": "active"},
            {"name": "Heavy Pulse Laser II", "slot": "high", "charge": "Imperial Navy Multifrequency M", "state": "active"},
            {"name": "Heavy Pulse Laser II", "slot": "high", "charge": "Imperial Navy Multifrequency M", "state": "active"},
            {"name": "Heavy Pulse Laser II", "slot": "high", "charge": "Imperial Navy Multifrequency M", "state": "active"},
            {"name": "10MN Afterburner II", "slot": "med", "state": "active"},
            {"name": "Cap Recharger II", "slot": "med", "state": "online"},
            {"name": "Cap Recharger II", "slot": "med", "state": "online"},
            {"name": "Heat Sink II", "slot": "low", "state": "online"},
            {"name": "Heat Sink II", "slot": "low", "state": "online"},
            {"name": "Heat Sink II", "slot": "low", "state": "online"},
            {"name": "Medium Armor Repairer II", "slot": "low", "state": "active"},
            {"name": "Multispectrum Energized Membrane II", "slot": "low", "state": "online"},
            {"name": "Damage Control II", "slot": "low", "state": "online"},
            {"name": "Medium Energy Metastasis Adjuster II", "slot": "rig", "state": "online"},
        ],
        "eft": """
            [Omen, Omen — Heavy Pulse]
            Heat Sink II
            Heat Sink II
            Heat Sink II
            Medium Armor Repairer II
            Multispectrum Energized Membrane II
            Damage Control II

            10MN Afterburner II
            Cap Recharger II
            Cap Recharger II

            Heavy Pulse Laser II, Imperial Navy Multifrequency M
            Heavy Pulse Laser II, Imperial Navy Multifrequency M
            Heavy Pulse Laser II, Imperial Navy Multifrequency M
            Heavy Pulse Laser II, Imperial Navy Multifrequency M
            Heavy Pulse Laser II, Imperial Navy Multifrequency M

            Medium Energy Metastasis Adjuster II
        """,
    },
    {
        "key": "vexor_drone",
        "name": "Vexor — drone boat (Hammerhead II x5)",
        "ship": "Vexor",
        "damage_pattern": [25, 25, 25, 25],
        "our_items": [
            {"name": "200mm Railgun II", "slot": "high", "charge": "Federation Navy Antimatter Charge M", "state": "active"},
            {"name": "200mm Railgun II", "slot": "high", "charge": "Federation Navy Antimatter Charge M", "state": "active"},
            {"name": "200mm Railgun II", "slot": "high", "charge": "Federation Navy Antimatter Charge M", "state": "active"},
            {"name": "10MN Afterburner II", "slot": "med", "state": "active"},
            {"name": "Cap Recharger II", "slot": "med", "state": "online"},
            {"name": "Cap Recharger II", "slot": "med", "state": "online"},
            {"name": "Drone Damage Amplifier II", "slot": "low", "state": "online"},
            {"name": "Drone Damage Amplifier II", "slot": "low", "state": "online"},
            {"name": "Drone Damage Amplifier II", "slot": "low", "state": "online"},
            {"name": "Medium Armor Repairer II", "slot": "low", "state": "active"},
            {"name": "Damage Control II", "slot": "low", "state": "online"},
            {"name": "Hammerhead II", "slot": "drone", "state": "active", "qty": 5},
        ],
        "eft": """
            [Vexor, Vexor — drone]
            Drone Damage Amplifier II
            Drone Damage Amplifier II
            Drone Damage Amplifier II
            Medium Armor Repairer II
            Damage Control II

            10MN Afterburner II
            Cap Recharger II
            Cap Recharger II

            200mm Railgun II, Federation Navy Antimatter Charge M
            200mm Railgun II, Federation Navy Antimatter Charge M
            200mm Railgun II, Federation Navy Antimatter Charge M


            Hammerhead II x5
        """,
    },
    {
        "key": "punisher_buffer",
        "name": "Punisher — 1600mm armor buffer",
        "ship": "Punisher",
        "damage_pattern": [25, 25, 25, 25],
        "our_items": [
            {"name": "Dual Light Pulse Laser II", "slot": "high", "charge": "Imperial Navy Multifrequency S", "state": "active"},
            {"name": "Dual Light Pulse Laser II", "slot": "high", "charge": "Imperial Navy Multifrequency S", "state": "active"},
            {"name": "Dual Light Pulse Laser II", "slot": "high", "charge": "Imperial Navy Multifrequency S", "state": "active"},
            {"name": "1MN Afterburner II", "slot": "med", "state": "active"},
            {"name": "Cap Recharger II", "slot": "med", "state": "online"},
            {"name": "1600mm Steel Plates II", "slot": "low", "state": "online"},
            {"name": "Multispectrum Energized Membrane II", "slot": "low", "state": "online"},
            {"name": "Thermal Energized Membrane II", "slot": "low", "state": "online"},
            {"name": "Damage Control II", "slot": "low", "state": "online"},
            {"name": "Heat Sink II", "slot": "low", "state": "online"},
            {"name": "Small Trimark Armor Pump II", "slot": "rig", "state": "online"},
            {"name": "Small Trimark Armor Pump II", "slot": "rig", "state": "online"},
            {"name": "Small Trimark Armor Pump II", "slot": "rig", "state": "online"},
        ],
        "eft": """
            [Punisher, Punisher — buffer]
            1600mm Steel Plates II
            Multispectrum Energized Membrane II
            Thermal Energized Membrane II
            Damage Control II
            Heat Sink II

            1MN Afterburner II
            Cap Recharger II

            Dual Light Pulse Laser II, Imperial Navy Multifrequency S
            Dual Light Pulse Laser II, Imperial Navy Multifrequency S
            Dual Light Pulse Laser II, Imperial Navy Multifrequency S

            Small Trimark Armor Pump II
            Small Trimark Armor Pump II
            Small Trimark Armor Pump II
        """,
    },
    {
        "key": "stabber_mwd",
        "name": "Stabber — 220mm AC + 50MN MWD (velocity/sig)",
        "ship": "Stabber",
        "damage_pattern": [25, 25, 25, 25],
        "our_items": [
            {"name": "220mm Vulcan AutoCannon II", "slot": "high", "charge": "Republic Fleet EMP M", "state": "active"},
            {"name": "220mm Vulcan AutoCannon II", "slot": "high", "charge": "Republic Fleet EMP M", "state": "active"},
            {"name": "220mm Vulcan AutoCannon II", "slot": "high", "charge": "Republic Fleet EMP M", "state": "active"},
            {"name": "220mm Vulcan AutoCannon II", "slot": "high", "charge": "Republic Fleet EMP M", "state": "active"},
            {"name": "50MN Microwarpdrive II", "slot": "med", "state": "active"},
            {"name": "Stasis Webifier II", "slot": "med", "state": "active"},
            {"name": "Cap Recharger II", "slot": "med", "state": "online"},
            {"name": "Gyrostabilizer II", "slot": "low", "state": "online"},
            {"name": "Gyrostabilizer II", "slot": "low", "state": "online"},
            {"name": "Damage Control II", "slot": "low", "state": "online"},
            {"name": "Tracking Enhancer II", "slot": "low", "state": "online"},
            {"name": "Medium Ancillary Current Router II", "slot": "rig", "state": "online"},
        ],
        "eft": """
            [Stabber, Stabber — MWD]
            Gyrostabilizer II
            Gyrostabilizer II
            Damage Control II
            Tracking Enhancer II

            50MN Microwarpdrive II
            Stasis Webifier II
            Cap Recharger II

            220mm Vulcan AutoCannon II, Republic Fleet EMP M
            220mm Vulcan AutoCannon II, Republic Fleet EMP M
            220mm Vulcan AutoCannon II, Republic Fleet EMP M
            220mm Vulcan AutoCannon II, Republic Fleet EMP M

            Medium Ancillary Current Router II
        """,
    },
]


# --------------------------------------------------------------------------- #
# Tolerance policy — one entry per compared stat.
#
# Every tolerance is JUSTIFIED by an identified source of legitimate difference; it is set
# just above that noise floor, never slack enough to hide a real modelling bug. A stat is
# WITHIN tolerance if |ours - pyfa| <= abs OR |ours - pyfa| <= rel * |pyfa|.
#
#   * FORCA telemetry rounds every number to 1 decimal at the boundary; pyfa is compared at
#     full precision. So a floor of ~0.1 absolute is pure output rounding, present on EVERY
#     stat, independent of magnitude.
#   * DPS/volley: the 0.1 rounding plus tiny float-order differences in the damage pipeline.
#     0.5% relative comfortably covers rounding at every DPS magnitude here (150–500) while
#     still catching a wrong multiplier (a missed 5%/10% bonus is >>0.5%).
#   * HP: pure attribute maths + rounding -> a flat 0.5 HP absolute.
#   * EHP: HP tolerance compounded with resonance rounding -> 0.5% relative or 1 HP.
#   * resist %: both sides round to 0.1pp; 0.2pp absolute covers double-rounding. Resists are
#     the same CCP data on both sides, so anything larger is a real stacking/skill bug.
#   * velocity / signature: attribute maths + 0.1 rounding -> 0.5% or 1 unit. Prop-on velocity
#     compares pyfa maxVelocity (prop folded in) against our propulsion_velocity.
#   * cap capacity: flat attribute + rounding -> 0.5.
#   * cap stable %: the recharge-equilibrium point. Both solve the same s(1-s) equation but
#     pyfa runs a discrete event cap SIM while we solve the closed form; 2.0pp covers the
#     model difference without hiding a wrong cap-need/recharge input.
#   * cap runtime (unstable): different integrators (pyfa event sim vs our 1s-step ODE) ->
#     10% or 5s.
# --------------------------------------------------------------------------- #
TOL = {
    "dps_total":       {"abs": 0.2, "rel": 0.005},
    "dps_weapon":      {"abs": 0.2, "rel": 0.005},
    "dps_drone":       {"abs": 0.2, "rel": 0.005},
    "volley_total":    {"abs": 0.2, "rel": 0.005},
    "hp.shield":       {"abs": 0.5, "rel": 0.0},
    "hp.armor":        {"abs": 0.5, "rel": 0.0},
    "hp.hull":         {"abs": 0.5, "rel": 0.0},
    "ehp.shield":      {"abs": 1.0, "rel": 0.005},
    "ehp.armor":       {"abs": 1.0, "rel": 0.005},
    "ehp.hull":        {"abs": 1.0, "rel": 0.005},
    "ehp_total":       {"abs": 1.0, "rel": 0.005},
    "velocity":        {"abs": 1.0, "rel": 0.005},
    "sig_radius":      {"abs": 1.0, "rel": 0.005},
    "cap_capacity":    {"abs": 0.5, "rel": 0.0},
    "cap_stable_pct":  {"abs": 2.0, "rel": 0.0},
    "cap_runtime_s":   {"abs": 5.0, "rel": 0.10},
}
# Resist stats (12 of them) all share one policy.
_RESIST_TOL = {"abs": 0.2, "rel": 0.0}


# --------------------------------------------------------------------------- #
# EFT parsing (harness-side; defines the pyfa input). Small, standalone — NOT pyfa's parser.
# --------------------------------------------------------------------------- #
def _parse_eft(eft_text):
    """Parse an EFT block into (ship, modules, drones).

    modules: list of (name, charge_or_None, state) where state is "active" (default) or
             "offline" ("/offline" suffix). drones: list of (name, qty).
    Section/stub lines ('[...]') and blanks are ignored; 'Name xN' lines are drones."""
    ship = None
    modules = []
    drones = []
    for raw in textwrap.dedent(eft_text).strip().splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("["):
            if ship is None:
                ship = line[1:-1].split(",")[0].strip()
            continue
        # drone/cargo "Name xN"
        if " x" in line and line.rsplit(" x", 1)[-1].isdigit():
            name, qty = line.rsplit(" x", 1)
            drones.append((name.strip(), int(qty)))
            continue
        state = "active"
        low = line.lower()
        if low.endswith("/offline"):
            state = "offline"
            line = line[:low.rfind("/offline")].rstrip().rstrip("/").rstrip()
        if "," in line:
            name, charge = line.split(",", 1)
            modules.append((name.strip(), charge.strip(), state))
        else:
            modules.append((line.strip(), None, state))
    return ship, modules, drones


def _consistency_guard(fit):
    """Fail loudly if the EFT text and the OUR items-blob describe different modules.

    Guards against the two representations drifting: they must list the same (name, charge)
    module multiset and the same (drone, qty) multiset. State/slot are intentionally not
    compared (EFT does not encode our online/active split or slot tokens)."""
    ship, mods, drones = _parse_eft(fit["eft"])
    if ship != fit["ship"]:
        raise ValueError(f"{fit['key']}: EFT ship {ship!r} != {fit['ship']!r}")
    eft_mods = Counter((n, c) for (n, c, _s) in mods)
    eft_drones = Counter((n, q) for (n, q) in drones)
    our_mods = Counter()
    our_drones = Counter()
    for it in fit["our_items"]:
        if it["slot"] == "drone":
            our_drones[(it["name"], int(it.get("qty", 1)))] += 1
        else:
            our_mods[(it["name"], it.get("charge"))] += 1
    if eft_mods != our_mods:
        raise ValueError(
            f"{fit['key']}: EFT vs items-blob module mismatch\n"
            f"  only in EFT:   {sorted(eft_mods - our_mods)}\n"
            f"  only in items: {sorted(our_mods - eft_mods)}")
    if eft_drones != our_drones:
        raise ValueError(
            f"{fit['key']}: EFT vs items-blob drone mismatch "
            f"{sorted(eft_drones.items())} != {sorted(our_drones.items())}")


# --------------------------------------------------------------------------- #
# pyfa side — embedded driver, run inside the pyfa venv. This is OUR code driving pyfa's
# public eos API headlessly (no wx, no service/gui layer). It reads a JSON list of fit
# specs on stdin and prints {key: stats} JSON on stdout between markers.
# --------------------------------------------------------------------------- #
PYFA_DRIVER = r'''
import json, os, sys, types

PYFA = os.path.abspath(sys.argv[1])
sys.path.insert(0, PYFA)
sys._called_from_test = True   # in-memory saveddata -> no migration copyfile
_stub = types.ModuleType("config")
_stub.debug = False
_stub.savePath = "/tmp/pyfa_headless_save"
_stub.saveDB = "/tmp/pyfa_headless_save/saveddata.db"
_stub.gameDB = os.path.join(PYFA, "eve.db")
_stub.pyfaPath = PYFA
sys.modules["config"] = _stub

import eos.db as eos_db
from eos.const import FittingModuleState as FMS
from eos.saveddata.fit import Fit
from eos.saveddata.ship import Ship
from eos.saveddata.module import Module
from eos.saveddata.character import Character
from eos.saveddata.drone import Drone
from eos.saveddata.damagePattern import DamagePattern

STATE = {"offline": FMS.OFFLINE, "online": FMS.ONLINE,
         "active": FMS.ACTIVE, "overheated": FMS.OVERHEATED}

def get_item(name):
    it = eos_db.getItem(name, eager=("attributes", "group.category", "effects"))
    if it is None:
        raise LookupError("pyfa: item not found: %r" % name)
    return it

def clamp_state(mod, desired):
    if mod.isValidState(desired):
        return desired
    for fb in (FMS.OVERHEATED, FMS.ACTIVE, FMS.ONLINE, FMS.OFFLINE):
        if fb <= desired and mod.isValidState(fb):
            return fb
    return FMS.ONLINE

def resist(ship, layer, dmg):
    prefix = "" if layer == "hull" else layer
    attr = "%s%sDamageResonance" % (prefix, dmg.capitalize())
    attr = attr[0].lower() + attr[1:]
    reso = ship.getModifiedItemAttr(attr)
    return None if reso is None else round((1.0 - reso) * 100.0, 3)

def build(spec):
    fit = Fit()
    fit.ship = Ship(get_item(spec["ship"]))
    fit.character = Character("All 5", 5)
    fit.damagePattern = DamagePattern(*spec.get("damage_pattern", [25, 25, 25, 25]))
    for name, charge, state in spec["modules"]:
        mod = Module(get_item(name))
        if charge:
            mod.charge = get_item(charge)
        mod.state = clamp_state(mod, STATE[state])
        fit.modules.append(mod)
        mod.owner = fit
    for name, qty in spec.get("drones", []):
        dr = Drone(get_item(name))
        dr.amount = qty
        dr.amountActive = qty
        fit.drones.append(dr)
        dr.owner = fit
    fit.calculateModifiedAttributes()
    return fit

def stats(fit):
    ship = fit.ship
    hp = fit.hp
    ehp = fit.ehp
    stable = fit.capStable
    state = fit.capState
    layers = ("shield", "armor", "hull")
    dmgs = ("em", "thermal", "kinetic", "explosive")
    return {
        "dps_total": round(float(fit.getTotalDps().total), 6),
        "dps_weapon": round(float(fit.getWeaponDps().total), 6),
        "dps_drone": round(float(fit.getDroneDps().total), 6),
        # Weapon (turret+missile) alpha only — matches our engine's "volley". pyfa's
        # getTotalVolley ALSO adds a notional drone volley; ours reports weapon alpha and
        # counts drones only in sustained DPS (drones have no synchronised alpha), so we
        # compare against the weapon volley. total volley kept for transparency.
        "volley_weapon": round(float(fit.getWeaponVolley().total), 6),
        "volley_total": round(float(fit.getTotalVolley().total), 6),
        "hp": {k: round(float(v), 6) for k, v in hp.items()},
        "ehp": {k: round(float(v), 6) for k, v in ehp.items()},
        "ehp_total": round(float(sum(ehp.values())), 6),
        "resists_pct": {L: {d: resist(ship, L, d) for d in dmgs} for L in layers},
        "max_velocity": round(float(ship.getModifiedItemAttr("maxVelocity")), 6),
        "sig_radius": round(float(ship.getModifiedItemAttr("signatureRadius")), 6),
        "cap_capacity": round(float(ship.getModifiedItemAttr("capacitorCapacity")), 6),
        "cap_stable": bool(stable),
        "cap_stable_pct": round(float(state), 6) if stable else None,
        "cap_runtime_s": None if stable else round(float(state), 6),
    }

specs = json.load(sys.stdin)
out = {}
for spec in specs:
    out[spec["key"]] = stats(build(spec))
print("PYFA_JSON_START")
print(json.dumps(out))
print("PYFA_JSON_END")
'''


# --------------------------------------------------------------------------- #
# our side — embedded eval, run via `docker compose run web python manage.py shell`.
# Read-only: resolves names -> type_ids, builds the items-blob, calls the services adapter
# with an All-V SkillProfile. Reads the fit list from the FIT_JSON env var.
# --------------------------------------------------------------------------- #
OUR_EVAL = r'''
import json, os
from apps.sde.models import SdeType
from apps.fitting import services
from apps.fitting.engine.types import SkillProfile

SPEC = json.loads(os.environ["FIT_JSON"])

def tid(name):
    return SdeType.objects.get(name=name, published=True).type_id

def build_items(spec):
    items = []
    for m in spec["our_items"]:
        if m["slot"] == "drone":
            items.append({"type_id": tid(m["name"]), "slot": "drone",
                          "state": "active", "quantity": int(m.get("qty", 1))})
            continue
        it = {"type_id": tid(m["name"]), "slot": m["slot"],
              "state": m.get("state", "active"), "quantity": int(m.get("qty", 1))}
        if m.get("charge"):
            it["charge_type_id"] = tid(m["charge"])
        items.append(it)
    return items

out = {}
for spec in SPEC:
    ship_id = tid(spec["ship"])
    items = build_items(spec)
    dp = spec.get("damage_pattern", [25, 25, 25, 25])
    op = services.operating_profile(
        propulsion=True,
        damage={"em": dp[0] / 100.0, "thermal": dp[1] / 100.0,
                "kinetic": dp[2] / 100.0, "explosive": dp[3] / 100.0})
    flat = services.evaluate(ship_id, items, SkillProfile.omniscient(), op, cached=False)
    off = flat.get("offence", {}) or {}
    dfc = flat.get("defence", {}) or {}
    cap = flat.get("capacitor", {}) or {}
    mob = flat.get("mobility", {}) or {}
    layers = dfc.get("layers", {}) or {}
    weapon = (round((off.get("turret_dps", 0) or 0) + (off.get("missile_dps", 0) or 0)
                    + (off.get("smartbomb_dps", 0) or 0) + (off.get("vorton_dps", 0) or 0)
                    + (off.get("breacher_dps", 0) or 0), 6))
    out[spec["key"]] = {
        "dps_total": off.get("total_dps"),
        "dps_weapon": weapon,
        "dps_drone": off.get("drone_dps"),
        "volley_total": off.get("volley"),
        "hp": {k: (layers.get(k, {}) or {}).get("hp") for k in ("shield", "armor", "hull")},
        "ehp": {k: (layers.get(k, {}) or {}).get("ehp") for k in ("shield", "armor", "hull")},
        "ehp_total": dfc.get("ehp_total"),
        "resists_pct": {k: (layers.get(k, {}) or {}).get("resists") for k in ("shield", "armor", "hull")},
        "max_velocity": mob.get("max_velocity"),
        "propulsion_velocity": mob.get("propulsion_velocity"),
        "sig_radius": mob.get("signature_radius"),
        "cap_capacity": cap.get("capacity"),
        "cap_stable": cap.get("stable"),
        "cap_stable_pct": cap.get("stable_pct"),
        "cap_runtime_s": cap.get("runtime_s"),
        "status": flat.get("status"),
        "unsupported": flat.get("unsupported"),
    }

print("OUR_JSON_START")
print(json.dumps(out, default=str))
print("OUR_JSON_END")
'''


# --------------------------------------------------------------------------- #
# Runners
# --------------------------------------------------------------------------- #
def _die(code, msg):
    print(msg, file=sys.stderr)
    sys.exit(code)


def _extract(marker_start, marker_end, blob):
    if marker_start not in blob or marker_end not in blob:
        return None
    return blob.split(marker_start, 1)[1].split(marker_end, 1)[0].strip()


def ensure_pyfa(pyfa_root, pyfa_venv):
    py = os.path.join(pyfa_venv, "bin", "python")
    if not os.path.isdir(pyfa_root) or not os.path.isfile(os.path.join(pyfa_root, "db_update.py")):
        _die(2, textwrap.dedent(f"""\
            pyfa clone not found at: {pyfa_root}
            WS-15a is a dev-machine differential gate; set pyfa up locally first:
              git clone https://github.com/pyfa-org/Pyfa {pyfa_root}
              python3.13 -m venv {pyfa_venv}
              {pyfa_venv}/bin/pip install "sqlalchemy==1.4.50" "logbook==1.7.0.post0"
            Or point --pyfa-root / --pyfa-venv at an existing setup."""))
    if not os.path.isfile(py):
        _die(2, f"pyfa venv python not found at: {py}\nCreate it (see --help / NOTES).")
    eve_db = os.path.join(pyfa_root, "eve.db")
    if not os.path.isfile(eve_db):
        print(f"[pyfa] eve.db missing — building from bundled staticdata ({eve_db}) ...",
              file=sys.stderr)
        builder = textwrap.dedent(f"""
            import os, sys, types
            PYFA = {pyfa_root!r}
            sys.path.insert(0, PYFA)
            sys._called_from_test = True
            s = types.ModuleType("config")
            s.debug = False; s.savePath = "/tmp/pyfa_headless_save"
            s.saveDB = "/tmp/pyfa_headless_save/saveddata.db"
            s.gameDB = os.path.join(PYFA, "eve.db"); s.pyfaPath = PYFA
            sys.modules["config"] = s
            import db_update; db_update.update_db()
        """)
        r = subprocess.run([py, "-c", builder], capture_output=True, text=True)
        if r.returncode != 0 or not os.path.isfile(eve_db):
            _die(2, "pyfa eve.db build failed:\n" + r.stdout + "\n" + r.stderr)
    return py


def run_pyfa(py, pyfa_root, fits):
    specs = []
    for fit in fits:
        _, mods, drones = _parse_eft(fit["eft"])
        specs.append({"key": fit["key"], "ship": fit["ship"],
                      "damage_pattern": fit.get("damage_pattern", [25, 25, 25, 25]),
                      "modules": mods, "drones": drones})
    r = subprocess.run([py, "-c", PYFA_DRIVER, pyfa_root],
                       input=json.dumps(specs), capture_output=True, text=True)
    payload = _extract("PYFA_JSON_START", "PYFA_JSON_END", r.stdout)
    if payload is None:
        _die(2, "pyfa side failed:\nSTDOUT:\n" + r.stdout + "\nSTDERR:\n" + r.stderr)
    return json.loads(payload)


def run_ours(fits):
    fit_json = json.dumps([{"key": f["key"], "ship": f["ship"],
                            "damage_pattern": f.get("damage_pattern", [25, 25, 25, 25]),
                            "our_items": f["our_items"]} for f in fits])
    env = dict(os.environ, FIT_JSON=fit_json)
    cmd = ["docker", "compose", "run", "--rm", "-T", "-e", "FIT_JSON",
           "web", "python", "manage.py", "shell", "-c", OUR_EVAL]
    r = subprocess.run(cmd, cwd=REPO_ROOT, env=env, capture_output=True, text=True)
    payload = _extract("OUR_JSON_START", "OUR_JSON_END", r.stdout)
    if payload is None:
        _die(2, "FORCA side failed (is the web container up? `docker compose ps`):\n"
                "STDOUT:\n" + r.stdout + "\nSTDERR:\n" + r.stderr)
    return json.loads(payload)


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #
def _within(ours, pyfa, tol):
    if ours is None or pyfa is None:
        return ours is None and pyfa is None
    diff = abs(float(ours) - float(pyfa))
    return diff <= tol["abs"] or (tol["rel"] > 0 and diff <= tol["rel"] * abs(float(pyfa)))


def _flatten_stats(fit, ours, pyfa):
    """Yield (stat_label, our_value, pyfa_value, tol) rows for one fit."""
    # pyfa folds an ACTIVE prop mod into ship.maxVelocity; our engine keeps max_velocity
    # prop-OFF and reports the prop-on figure as propulsion_velocity (== max_velocity when
    # there is no prop). So compare pyfa maxVelocity against our propulsion_velocity always.
    our_vel = ours.get("propulsion_velocity")
    rows = [
        ("dps_total", ours.get("dps_total"), pyfa.get("dps_total"), TOL["dps_total"]),
        ("dps_weapon", ours.get("dps_weapon"), pyfa.get("dps_weapon"), TOL["dps_weapon"]),
        ("dps_drone", ours.get("dps_drone"), pyfa.get("dps_drone"), TOL["dps_drone"]),
        # our "volley" is weapon alpha only -> compare against pyfa's weapon volley.
        ("volley_weapon", ours.get("volley_total"), pyfa.get("volley_weapon"), TOL["volley_total"]),
        ("hp.shield", ours["hp"].get("shield"), pyfa["hp"].get("shield"), TOL["hp.shield"]),
        ("hp.armor", ours["hp"].get("armor"), pyfa["hp"].get("armor"), TOL["hp.armor"]),
        ("hp.hull", ours["hp"].get("hull"), pyfa["hp"].get("hull"), TOL["hp.hull"]),
        ("ehp.shield", ours["ehp"].get("shield"), pyfa["ehp"].get("shield"), TOL["ehp.shield"]),
        ("ehp.armor", ours["ehp"].get("armor"), pyfa["ehp"].get("armor"), TOL["ehp.armor"]),
        ("ehp.hull", ours["ehp"].get("hull"), pyfa["ehp"].get("hull"), TOL["ehp.hull"]),
        ("ehp_total", ours.get("ehp_total"), pyfa.get("ehp_total"), TOL["ehp_total"]),
        ("velocity", our_vel, pyfa.get("max_velocity"), TOL["velocity"]),
        ("sig_radius", ours.get("sig_radius"), pyfa.get("sig_radius"), TOL["sig_radius"]),
        ("cap_capacity", ours.get("cap_capacity"), pyfa.get("cap_capacity"), TOL["cap_capacity"]),
    ]
    for layer in ("shield", "armor", "hull"):
        our_r = ours.get("resists_pct", {}).get(layer) or {}
        pf_r = pyfa.get("resists_pct", {}).get(layer) or {}
        for d in ("em", "thermal", "kinetic", "explosive"):
            rows.append((f"resist.{layer}.{d}", our_r.get(d), pf_r.get(d), _RESIST_TOL))
    # Cap: compare stability class first, then the meaningful sub-metric.
    o_stable, p_stable = ours.get("cap_stable"), pyfa.get("cap_stable")
    rows.append(("cap_stable", o_stable, p_stable, None))  # bool compare
    if o_stable and p_stable:
        rows.append(("cap_stable_pct", ours.get("cap_stable_pct"),
                     pyfa.get("cap_stable_pct"), TOL["cap_stable_pct"]))
    elif (not o_stable) and (not p_stable):
        rows.append(("cap_runtime_s", ours.get("cap_runtime_s"),
                     pyfa.get("cap_runtime_s"), TOL["cap_runtime_s"]))
    return rows


def _fmt(v):
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "T" if v else "F"
    if isinstance(v, (int, float)):
        return f"{v:.3f}"
    return str(v)


def compare(fit, ours, pyfa):
    expect = fit.get("expect", {})
    rows = _flatten_stats(fit, ours, pyfa)
    results = []
    failures = 0
    for label, ov, pv, tol in rows:
        if tol is None:  # boolean equality (cap_stable)
            ok = (ov == pv)
        else:
            ok = _within(ov, pv, tol)
        verdict = "ok" if ok else "FAIL"
        reason = expect.get(label)
        if not ok and reason:
            verdict = "explained"
        if verdict == "FAIL":
            failures += 1
        results.append((label, ov, pv, verdict, reason))
    return results, failures


def print_table(fit, results):
    print(f"\n=== {fit['key']}: {fit['name']} ===")
    print(f"  {'stat':<22}{'ours':>13}{'pyfa':>13}{'delta':>12}  verdict")
    print("  " + "-" * 72)
    for label, ov, pv, verdict, reason in results:
        if isinstance(ov, (int, float)) and isinstance(pv, (int, float)) \
                and not isinstance(ov, bool):
            delta = f"{abs(float(ov) - float(pv)):.3f}"
        else:
            delta = "-"
        flag = {"ok": "", "explained": "  (explained)", "FAIL": "  <== FAIL"}[verdict]
        line = f"  {label:<22}{_fmt(ov):>13}{_fmt(pv):>13}{delta:>12}  {verdict}{flag}"
        print(line)
        if reason and verdict != "ok":
            print(f"      reason: {reason}")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pyfa-root", default=DEFAULT_PYFA_ROOT,
                    help=f"pyfa clone dir (default: {DEFAULT_PYFA_ROOT})")
    ap.add_argument("--pyfa-venv", default=DEFAULT_PYFA_VENV,
                    help=f"pyfa venv dir (default: {DEFAULT_PYFA_VENV})")
    ap.add_argument("--fits", default="",
                    help="comma-separated fit keys to run (default: all)")
    ap.add_argument("--json", action="store_true",
                    help="dump the raw ours/pyfa stat JSON per fit and exit 0")
    args = ap.parse_args()

    fits = FITS
    if args.fits:
        want = {k.strip() for k in args.fits.split(",") if k.strip()}
        fits = [f for f in FITS if f["key"] in want]
        missing = want - {f["key"] for f in fits}
        if missing:
            _die(2, f"unknown fit key(s): {sorted(missing)}; "
                    f"available: {[f['key'] for f in FITS]}")
    if not fits:
        _die(2, "no fits selected")

    # Cross-check the two representations before spending time on evaluation.
    for fit in fits:
        _consistency_guard(fit)

    py = ensure_pyfa(args.pyfa_root, args.pyfa_venv)
    print(f"[1/2] evaluating {len(fits)} fit(s) through pyfa (eos) ...", file=sys.stderr)
    pyfa_all = run_pyfa(py, args.pyfa_root, fits)
    print("[2/2] evaluating through the FORCA engine (web container) ...", file=sys.stderr)
    ours_all = run_ours(fits)

    if args.json:
        for fit in fits:
            print(json.dumps({"key": fit["key"],
                              "ours": ours_all.get(fit["key"]),
                              "pyfa": pyfa_all.get(fit["key"])}, indent=2))
        return 0

    total_failures = 0
    for fit in fits:
        ours = ours_all.get(fit["key"])
        pyfa = pyfa_all.get(fit["key"])
        if ours is None or pyfa is None:
            print(f"\n=== {fit['key']} ===  MISSING RESULT "
                  f"(ours={ours is not None}, pyfa={pyfa is not None})")
            total_failures += 1
            continue
        if ours.get("status") not in (None, "valid", "warning"):
            print(f"  note: FORCA status={ours.get('status')} "
                  f"unsupported={ours.get('unsupported')}", file=sys.stderr)
        results, failures = compare(fit, ours, pyfa)
        print_table(fit, results)
        total_failures += failures

    print("\n" + "=" * 76)
    if total_failures:
        print(f"RESULT: {total_failures} unexplained out-of-tolerance stat(s). "
              f"Investigate (possible engine bug or a tolerance/model gap to document).")
        return 1
    print("RESULT: all stats within tolerance (or explained). Differential gate PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
