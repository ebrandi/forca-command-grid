"""Named dogma attribute / effect ids and the groupings the engine reasons about.

The numeric ids are the public CCP Static Data Export ``dgmAttributeTypes`` /
``dgmEffects`` identifiers (the same ids the rest of the SDE import uses). They are
reference constants, not third-party code; using the real ids means the engine runs
unchanged against a full SDE import and against the feature's own test fixtures.

Only the attributes the engine actually consumes are named here; the loader stores
every attribute it is given, so unnamed ones remain available for future mechanics.
"""
from __future__ import annotations

# --- Fitting resources ------------------------------------------------------
CPU_OUTPUT = 48          # ship CPU
POWER_OUTPUT = 11        # ship powergrid
CALIBRATION = 1132       # ship rig calibration capacity (upgradeCapacity)
CPU_USAGE = 50           # module CPU cost
POWER_USAGE = 30         # module powergrid cost
CALIBRATION_COST = 1153  # rig calibration cost (upgradeCost)
RIG_SIZE = 1547          # ship/rig rig size class

HI_SLOTS = 14
MED_SLOTS = 13
LOW_SLOTS = 12
RIG_SLOTS = 1137
TURRET_HARDPOINTS = 102  # turretSlotsLeft
LAUNCHER_HARDPOINTS = 101  # launcherSlotsLeft
DRONE_BANDWIDTH = 1271
DRONE_CAPACITY = 283
LAUNCHER_HARDPOINT_MOD = 102

# --- Hitpoints & resistances ------------------------------------------------
HULL_HP = 9
ARMOR_HP = 265
SHIELD_HP = 263
CAPACITY_CARGO = 38

# resonance = 1 - resist.  Lower resonance is better.  Grouped for EHP maths.
SHIELD_RESONANCE = {
    "em": 271, "thermal": 274, "kinetic": 273, "explosive": 272,
}
ARMOR_RESONANCE = {
    "em": 267, "thermal": 270, "kinetic": 269, "explosive": 268,
}
HULL_RESONANCE = {
    "em": 113, "thermal": 110, "kinetic": 109, "explosive": 111,
}
DAMAGE_TYPES = ("em", "thermal", "kinetic", "explosive")

# --- Capacitor --------------------------------------------------------------
CAP_CAPACITY = 482
CAP_RECHARGE_RATE = 55   # ms for a full recharge
CAP_NEED = 6             # capacitorNeed (module activation cost)

# --- Shield/armor local tank ------------------------------------------------
SHIELD_RECHARGE_RATE = 479  # ms
SHIELD_BOOST_AMOUNT = 68     # shieldBonus per cycle
ARMOR_REPAIR_AMOUNT = 84     # armorDamageAmount per cycle
CYCLE_TIME = 73              # duration (ms)

# --- Offence ----------------------------------------------------------------
DAMAGE_MULTIPLIER = 64       # turret/launcher damage multiplier
RATE_OF_FIRE = 51            # speed (ms between cycles)
DRONE_DAMAGE_MULTIPLIER = 64
# charge / projectile damage components
EM_DAMAGE = 114
EXPLOSIVE_DAMAGE = 116
KINETIC_DAMAGE = 117
THERMAL_DAMAGE = 118
CHARGE_DAMAGE = {
    "em": EM_DAMAGE, "thermal": THERMAL_DAMAGE,
    "kinetic": KINETIC_DAMAGE, "explosive": EXPLOSIVE_DAMAGE,
}
OPTIMAL_RANGE = 54           # maxRange
FALLOFF = 158
TRACKING_SPEED = 160

# --- Mobility / signature ---------------------------------------------------
MASS = 4
AGILITY = 70                 # inertiaModifier
MAX_VELOCITY = 37
SIGNATURE_RADIUS = 552
WARP_SPEED_MULT = 600        # warpSpeedMultiplier
SPEED_BONUS = 20             # afterburner/MWD speedFactor (% velocity bonus)
SPEED_BOOST_FACTOR = 567     # speedBoostFactor
SIGNATURE_RADIUS_BONUS = 554
MASS_ADDITION = 796          # MWD mass addition (massAddition)

# --- Targeting / sensors ----------------------------------------------------
MAX_TARGET_RANGE = 76
MAX_LOCKED_TARGETS = 192
SCAN_RESOLUTION = 564
SENSOR_STRENGTHS = {
    "radar": 208, "ladar": 209, "magnetometric": 210, "gravimetric": 211,
}

# --- Requirements (skills to use a type) ------------------------------------
REQUIRED_SKILLS = [(182, 277), (183, 278), (184, 279), (1285, 1286), (1289, 1290), (1290, 1287)]

# Attributes whose module modifiers suffer the stacking penalty when several apply
# to the same target attribute. Resonances (resists), tracking and speed bonuses are
# penalised; raw capacity/cpu/pg/hp are not. Kept explicit so the engine never guesses.
STACKING_PENALISED_ATTRIBUTES = frozenset(
    set(SHIELD_RESONANCE.values())
    | set(ARMOR_RESONANCE.values())
    | set(HULL_RESONANCE.values())
    | {MAX_VELOCITY, TRACKING_SPEED, SIGNATURE_RADIUS, SCAN_RESOLUTION}
)

# --- Slot-defining effects (dgmEffects) — which rack a module occupies --------
EFFECT_LO_POWER = 11
EFFECT_HI_POWER = 12
EFFECT_MED_POWER = 13
EFFECT_RIG_SLOT = 2663
EFFECT_SUBSYSTEM = 3772
SLOT_EFFECTS = {
    EFFECT_HI_POWER: "high", EFFECT_MED_POWER: "med", EFFECT_LO_POWER: "low",
    EFFECT_RIG_SLOT: "rig", EFFECT_SUBSYSTEM: "subsystem",
}

# --- Effect categories (dgmEffects.effectCategory) --------------------------
EFFECT_PASSIVE = 0
EFFECT_ACTIVE = 1
EFFECT_TARGET = 2
EFFECT_AREA = 3
EFFECT_ONLINE = 4
EFFECT_OVERLOAD = 5

# --- Well-known group / category ids ---------------------------------------
CATEGORY_CHARGE = 8
CATEGORY_MODULE = 7
CATEGORY_DRONE = 18
CATEGORY_SHIP = 6
CATEGORY_SUBSYSTEM = 32
CATEGORY_IMPLANT = 20
