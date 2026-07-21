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
# Active shield hardeners carry their resist as a percentage RESISTANCE BONUS (e.g. -32.5%)
# on dedicated attrs, applied postPercent to the ship's shield resonance — NOT as a resonance
# value on 271-274. (val<0 improves the resist: factor = 1 + val/100.)
SHIELD_RESIST_BONUS = {"em": 984, "explosive": 985, "kinetic": 986, "thermal": 987}
DAMAGE_TYPES = ("em", "thermal", "kinetic", "explosive")

# --- Module bonus SOURCE attributes -----------------------------------------
# Real SDE modules carry their bonus on a SEPARATE attribute applied to the ship's layer via a
# dogma effect — NOT on the layer attribute itself. The engine must read these source ids, not
# the target id, or the bonus reads as zero.
SHIELD_EXTENDER_HP_BONUS = 72   # capacityBonus — flat shield HP add (Shield Extenders)
ARMOR_PLATE_HP_BONUS = 1159     # armorHPBonus — flat armour HP add (plates)
SHIELD_RIG_HP_BONUS = 337       # shieldCapacityBonus (%) — Core Defense Field Extender rigs
STRUCTURE_HP_MULTIPLIER = 150   # structureHPMultiplier (<1 = penalty, e.g. nanofibers)
SIG_RADIUS_ADD = 983            # signatureRadiusAdd — flat sig add (Shield Extenders)
# Damage Control / Assault DCU store HULL (structure) resonance on their OWN attr ids, distinct
# from the ship's structure resonance (109/110/111/113).
HULL_RESONANCE_MODULE = {"em": 974, "explosive": 975, "kinetic": 976, "thermal": 977}
RESISTANCE_MULTIPLIER = 2746    # Assault DCU overload: uniform resonance multiplier when active
# Mobility module modifiers.
AGILITY_MULTIPLIER = 169        # agilityMultiplier (%) — nanofibers etc. (stacking penalised)
VELOCITY_BONUS_MOD = 1076       # velocity % bonus from nanofibers (stacking penalised)
MWD_SIG_ROLE_BONUS = 1803       # hull role bonus reducing the MWD signature penalty (%)

# --- T3 subsystem contributions (a fitted subsystem ADDS these to the hull) -----------------
# Strategic-cruiser subsystems carry CPU/PG output on the normal output attrs (48/11) and add
# slots/hardpoints/structure-HP via dedicated modifier attrs.
SUB_HI_SLOT_MOD = 1374          # hiSlotModifier
SUB_MED_SLOT_MOD = 1375         # medSlotModifier
SUB_LOW_SLOT_MOD = 1376         # lowSlotModifier
SUB_TURRET_HP_MOD = 1368        # turretHardPointModifier
SUB_LAUNCHER_HP_MOD = 1369      # launcherHardPointModifier
SUB_STRUCTURE_HP_ADD = 2688     # structureHPBonusAdd
SUB_DRONE_BANDWIDTH_ADD = 1271  # droneBandwidth (added passively)
SUB_DRONE_CAPACITY_ADD = 283    # droneCapacity (added passively)

# --- Module bonus effect ids (dgmEffects) — used to classify a module's effect ---------------
EFFECT_SHIELD_EXTENDER = 21     # shieldCapacityBonusOnline
EFFECT_CDFE_RIG = 446           # shield-extender rig postPercent
EFFECT_STRUCTURE_HP = 60        # structureHPMultiply (nanofiber)
EFFECT_DAMAGE_CONTROL = 2302    # damageControl (DCU family, resonance stacking-exempt)
EFFECT_ASSAULT_DCU = 7012       # moduleBonusAssaultDamageControl

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
# A damage-mod module (gyro/magstab/heat sink/BCS) carries its RATE-OF-FIRE bonus on a
# SEPARATE attribute (speedMultiplier, <1 = faster) — NOT on attr 51. Reading attr 51 off the
# mod finds nothing, so the RoF bonus is silently dropped unless this attribute is used.
ROF_MULTIPLIER = 204         # speedMultiplier (damage-mod rate-of-fire bonus)
DRONE_DAMAGE_MULTIPLIER = 64
# A Ballistic Control System boosts MISSILE damage via a dedicated bonus attribute (missiles
# take damage from the charge, so a launcher/BCS has no plain damageMultiplier attr 64).
MISSILE_DAMAGE_MULT_BONUS = 213   # missileDamageMultiplierBonus (BCS)
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
# Turret/drone application (hit-quality maths). optimalSigRadius (620) is the gun's
# "signature resolution": the target sig at which tracking is perfect — verified as the
# canonical name (there is NO attribute called signatureResolution). entityCruiseSpeed
# (508) is a drone's autonomous cruise speed; a value of 0 (alongside maxVelocity≈0)
# marks a sentry drone, which applies like a stationary turret rather than chasing.
OPTIMAL_SIG_RADIUS = 620
ENTITY_CRUISE_SPEED = 508
# Overload (overheat) bonuses applied only when a weapon is in the OVERHEATED state.
OVERLOAD_ROF_BONUS = 1205    # overloadRofBonus (turret & launcher, negative = faster)
OVERLOAD_DAMAGE_BONUS = 1210  # overloadDamageModifier (turrets)

# --- Weapon-identifying effects (robust across all weapon groups) -----------
# Verified against live dgmEffects names: effect 34 is projectileFired (hybrid +
# projectile turrets) and effect 10 is targetAttack (ENERGY turrets — lasers). The
# previous single constant (34, mislabelled "targetAttack") missed lasers entirely,
# so every laser fit computed zero DPS. Missile launchers carry useMissiles.
EFFECT_TURRET = 34            # projectileFired (hybrids + projectiles)
EFFECT_LASER = 10             # targetAttack (energy turrets)
TURRET_EFFECTS = frozenset({EFFECT_TURRET, EFFECT_LASER})
EFFECT_LAUNCHER = 101         # useMissiles

# --- Charge compatibility (which ammo a weapon accepts) ---------------------
# A weapon lists the charge GROUPS it accepts (chargeGroup1..5) and a charge SIZE
# (small/medium/large/…); a charge fits when its group is accepted and its size matches.
CHARGE_GROUP_ATTRS = (604, 605, 606, 609, 610)   # chargeGroup1..chargeGroup5
CHARGE_SIZE = 128                                 # chargeSize (weapon + charge)

# --- Missile application (attributes live on the missile CHARGE) -------------
# Verified against the live dgmAttributeTypes names: 655 is aoeFALLOFF (a range, ~1500m
# on every missile — the previous constants misread it as the DRF), and since the 2015
# missile rework attr 1353 aoeDamageReductionFactor holds the application EXPONENT
# directly (~0.5-1.0); 1354 (sensitivity) is legacy, default 1.0.
AOE_CLOUD_SIZE = 654          # explosion radius (m)
AOE_VELOCITY = 653            # explosion velocity (m/s)
AOE_FALLOFF = 655
AOE_DAMAGE_REDUCTION_FACTOR = 1353       # the application exponent itself
AOE_DAMAGE_REDUCTION_SENSITIVITY = 1354  # legacy (pre-rework), unused by the formula

# --- Electronic warfare (module strengths, for the utility/EWAR readout) -----
# Every id below is a live query result (scout-data §D, re-verified 2026-07-21) — never a
# guess. Two were previously wrong and made their readout silently zero: warpScrambleStrength
# is 105 (504 = entityWarpScrambleChance, an NPC-only attr absent on player scramblers) and
# the damp scan-res attr is 566 scanResolutionBonus (565 = scanResolutionMultiplier, absent
# on player dampeners).
WARP_SCRAMBLE_STRENGTH = 105            # warpScrambleStrength — warp core strength neutralised
ENERGY_NEUTRALISER_AMOUNT = 97          # GJ removed per cycle
POWER_TRANSFER_AMOUNT = 90              # GJ drained per cycle (nosferatu)
ECM_STRENGTH = {                        # racial ECM jam strength bonuses (scanXStrengthBonus)
    "gravimetric": 238, "ladar": 239, "magnetometric": 240, "radar": 241,
}
EW_TARGET_JAM = 831                     # ewTargetJam (flags an ECM module; 0.0 default)
ECM_BURST_RANGE = 142                   # ecmBurstRange (AoE radius of a burst jammer)
MAX_TARGET_RANGE_BONUS = 309            # remote sensor damp: lock-range reduction (%)
SCAN_RESOLUTION_BONUS = 566             # remote sensor damp: scan-res reduction (%) — 566 not 565
SIGNATURE_RADIUS_BONUS_ATTR = 554       # target painter: target sig increase (%)
# Tracking disruptor (shipModuleTrackingDisruptor 6424) output attributes.
TRACKING_SPEED_BONUS = 767              # trackingSpeedBonus (%)
TD_MAX_RANGE_BONUS = 351                # maxRangeBonus (turret optimal, %)
TD_FALLOFF_BONUS = 349                  # falloffBonus (turret falloff, %)
# Guidance disruptor (shipModuleGuidanceDisruptor 6423) output attributes.
MISSILE_VELOCITY_BONUS = 547            # missileVelocityBonus (%)
EXPLOSION_DELAY_BONUS = 596             # explosionDelayBonus (missile flight time, %)
AOE_VELOCITY_BONUS = 847                # aoeVelocityBonus (explosion velocity, %)
AOE_CLOUD_SIZE_BONUS = 848             # aoeCloudSizeBonus (explosion radius, %)

# --- Mobility / signature ---------------------------------------------------
MASS = 4
AGILITY = 70                 # inertiaModifier
MAX_VELOCITY = 37
SIGNATURE_RADIUS = 552
WARP_SPEED_MULT = 600        # warpSpeedMultiplier
BASE_WARP_SPEED = 1281       # baseWarpSpeed (AU/s = baseWarpSpeed × warpSpeedMultiplier)
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
# (requiredSkillN, requiredSkillNLevel) attribute-id pairs, verified against the live
# dgmAttributeTypes names: skill5's level is 1287 and skill6's is 1288 (the previous
# pairs (1289,1290)/(1290,1287) mis-read skill6's TYPE id as skill5's level).
REQUIRED_SKILLS = [(182, 277), (183, 278), (184, 279), (1285, 1286), (1289, 1287), (1290, 1288)]
REQUIRED_SKILL_ATTRS = tuple(sid for sid, _lvl in REQUIRED_SKILLS)

# Attributes whose module modifiers suffer the stacking penalty when several apply
# to the same target attribute. Resonances (resists), tracking and speed bonuses are
# penalised; raw capacity/cpu/pg/hp are not. Kept explicit so the engine never guesses.
STACKING_PENALISED_ATTRIBUTES = frozenset(
    set(SHIELD_RESONANCE.values())
    | set(ARMOR_RESONANCE.values())
    | set(HULL_RESONANCE.values())
    | {MAX_VELOCITY, TRACKING_SPEED, SIGNATURE_RADIUS, SCAN_RESOLUTION}
)

# --- Fit-validation restriction attributes (WS-3) ---------------------------
# Verified live against dgmAttributeTypes (scout-data §A). Modules carry these to
# constrain where/how many can be fitted; absence of an attribute means "unrestricted".
MAX_GROUP_ACTIVE = 763       # most modules of this group that may be ACTIVE at once
MAX_GROUP_ONLINE = 978       # most modules of this group that may be ONLINE at once
MAX_GROUP_FITTED = 1544      # most modules of this group that may be FITTED at all
FITS_TO_SHIP_TYPE = 1380     # module fits ONLY this exact hull type
IMPLANTNESS = 331            # the head slot an implant occupies (two sharing it conflict)
BOOSTERNESS = 1087           # the slot a combat booster occupies (two sharing it conflict)
SUBSYSTEM_SLOT = 1366        # the subsystem slot a T3C subsystem occupies
MAX_SUBSYSTEMS = 1367        # hull attr present ⇒ a Strategic Cruiser (value is STALE — see
#                              adapter.subsystem_slots_for_hull for the real slot count)
# canFitShipGroup01..20 (zero-padded names) — hull GROUP whitelist. Non-contiguous ids.
CAN_FIT_SHIP_GROUP_ATTRS = (1298, 1299, 1300, 1301, 1872, 1879, 1880, 1881, 2065, 2396,
                            2476, 2477, 2478, 2479, 2480, 2481, 2482, 2483, 2484, 2485)
# canFitShipType1..12 (NOT zero-padded) — hull TYPE whitelist. Non-contiguous ids.
CAN_FIT_SHIP_TYPE_ATTRS = (1302, 1303, 1304, 1305, 1944, 2103, 2463, 2486, 2487, 2488,
                           2758, 5948)

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
