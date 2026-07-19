"""Ship / role / skill bonus specification and a curated standard-skill set.

A :class:`BonusSpec` is FORCA's normalised representation of a bonus: "this source
changes this attribute on items matching this filter, by this much, scaled by a skill
level (or not)". Ship and role bonuses come from the data provider (loaded per hull);
the universal skill bonuses below are game constants encoded as data.

Only bonuses that are covered by an engine test are relied upon for headline numbers;
the set is intentionally small and each entry cites the in-game skill it models. New
bonuses are added here, the single documented place, never inline in the evaluator.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import attributes as A
from .effects import Op


@dataclass(frozen=True)
class BonusSpec:
    key: str
    target_attr: int
    amount: float                     # percent (per level if per_level, else flat)
    target_domain: str = "item"       # "ship" | "item"
    skill_id: int | None = None       # None => always-on (ship/role bonus)
    per_level: bool = False
    match_group_ids: tuple[int, ...] = ()
    match_category_ids: tuple[int, ...] = ()
    match_attr_present: int | None = None
    match_effect_id: int | None = None   # module must carry this dogma effect (weapon detection)
    match_required_skill_id: int | None = None  # item/charge must REQUIRE this skill (hull bonus filter)
    penalised: bool = False
    op: Op = Op.MULTIPLY
    label: str = ""

    def factor(self, level: int) -> float:
        """Multiplicative factor for this bonus at the given skill level."""
        pct = self.amount * (level if self.per_level else 1)
        return 1.0 + pct / 100.0


# --- Well-known skill ids (public game data) --------------------------------
SKILL_SURGICAL_STRIKE = 3315      # +3% turret damage / level
SKILL_RAPID_FIRING = 3310         # -4% turret rate-of-fire time / level
SKILL_SHIELD_MANAGEMENT = 3419    # +5% shield capacity / level
SKILL_HULL_UPGRADES = 3394        # +5% armor HP / level
SKILL_MECHANICS = 3392            # +5% structure HP / level
SKILL_NAVIGATION = 3449           # +5% max velocity / level
SKILL_CAP_MANAGEMENT = 3418       # +5% capacitor capacity / level
SKILL_CAP_SYSTEMS_OP = 3417       # -5% capacitor recharge time / level
SKILL_WARHEAD_UPGRADES = 20315    # +2% missile (all types) damage / level (was wrongly 3317)
# Missile damage/RoF skills. Damage skills modify the CHARGE's damage attributes (missiles take
# all damage from the charge), scoped to charges requiring the skill; Rapid Launch cuts launcher RoF.
SKILL_HEAVY_MISSILES = 3324       # +5% heavy-missile damage / level
SKILL_MISSILE_LAUNCHER_OP = 3319  # base missile skill (every missile charge requires it)
SKILL_RAPID_LAUNCH = 21071        # -3% missile launcher rate-of-fire time / level
# Engineering / fitting skills — these decide whether a loadout actually FITS. Without them
# every fit is checked against the untrained hull's base CPU/PG, so real (skilled) fits read
# as over-capacity. CPU/PG Management raise the ship's output; the Weapon Upgrades pair lower
# a weapon's fitting cost.
SKILL_CPU_MANAGEMENT = 3426       # +5% ship CPU output / level
SKILL_POWER_GRID_MANAGEMENT = 3413  # +5% ship powergrid output / level
SKILL_WEAPON_UPGRADES = 3318      # -5% CPU need of turrets & launchers / level
SKILL_ADVANCED_WEAPON_UPGRADES = 11207  # -2% powergrid need of turrets & launchers / level

# Gunnery support (turrets — matched by the turret effect or a specific required-skill id).
SKILL_GUNNERY = 3300              # -2% turret rate-of-fire time / level
SKILL_MOTION_PREDICTION = 3312    # +5% turret tracking / level
SKILL_SHARPSHOOTER = 3311         # +5% turret optimal range / level
SKILL_TRAJECTORY_ANALYSIS = 3317  # +5% turret falloff / level
# Base turret damage skills (+5% dmg/level) and specialisations (+2% dmg/level). Each is scoped
# to weapons that REQUIRE it, so a gun only receives the skills that gate it.
SKILL_SMALL_PROJECTILE = 3302
SKILL_MEDIUM_PROJECTILE = 3305
SKILL_LARGE_PROJECTILE = 3308
SKILL_MEDIUM_AC_SPEC = 12208
# Drones (damage): Drone Interfacing scales ALL drones; the size/racial skills scope by req-skill.
SKILL_DRONE_INTERFACING = 3442    # +10% drone damage / level (all drones)
# Size drone-operation skills (+5% damage/level, scoped by the drone's required size skill).
SKILL_LIGHT_DRONE_OP = 24241
SKILL_MEDIUM_DRONE_OP = 33699
SKILL_HEAVY_DRONE_OP = 3441
# Racial drone specialisations (+2% damage/level, scoped by required skill).
SKILL_AMARR_DRONE_SPEC = 12484
SKILL_MINMATAR_DRONE_SPEC = 12485
SKILL_GALLENTE_DRONE_SPEC = 12486
SKILL_CALDARI_DRONE_SPEC = 12487
# Navigation / targeting / shield-fitting.
SKILL_EVASIVE_MANEUVERING = 3453  # -5% ship agility (inertia) / level
SKILL_SPACESHIP_COMMAND = 3327    # -2% ship agility (inertia) / level
SKILL_ACCELERATION_CONTROL = 3452  # +5% AB/MWD velocity factor / level
SKILL_LONG_RANGE_TARGETING = 3428  # +5% max targeting range / level
SKILL_SIGNATURE_ANALYSIS = 3431   # +5% scan resolution / level
SKILL_SHIELD_UPGRADES = 3425      # -5% powergrid need of shield modules / level

# Prop-module (afterburner / MWD) inventory groups, for the velocity-factor skill match.
PROP_GROUPS = (46, 47)

# Group ids used by the standard bonuses (public SDE group ids).
GROUP_PROJECTILE_TURRET = 55
GROUP_HYBRID_TURRET = 74
GROUP_ENERGY_TURRET = 53
TURRET_GROUPS = (GROUP_PROJECTILE_TURRET, GROUP_HYBRID_TURRET, GROUP_ENERGY_TURRET)
# Missile launcher groups (rocket/light/heavy/cruise/torpedo/rapid variants).
LAUNCHER_GROUPS = (507, 508, 509, 510, 511, 524, 771, 1245, 1246)


# The curated, engine-supported standard skill bonuses. Each is validated by a test that
# hand-computes the expected number; see tests/test_fitting_engine.py.
STANDARD_SKILL_BONUSES: tuple[BonusSpec, ...] = (
    # Surgical Strike is a GUNNERY skill — turrets only. Scope it by the turret effect so it
    # cannot leak onto drones (category 18, which also carry attr 64) now that the drone loop
    # routes through the bonus engine.
    BonusSpec("surgical_strike", A.DAMAGE_MULTIPLIER, 3.0, skill_id=SKILL_SURGICAL_STRIKE,
              per_level=True, match_effect_id=A.EFFECT_TURRET, label="Surgical Strike"),
    # Rapid Firing is a Gunnery skill — turrets only, matched by the targetAttack effect
    # (launchers also carry a rate-of-fire attr, so an attr-presence match would over-apply).
    BonusSpec("rapid_firing", A.RATE_OF_FIRE, -4.0, skill_id=SKILL_RAPID_FIRING,
              per_level=True, match_effect_id=A.EFFECT_TURRET, label="Rapid Firing"),
    BonusSpec("shield_management", A.SHIELD_HP, 5.0, target_domain="ship",
              skill_id=SKILL_SHIELD_MANAGEMENT, per_level=True, label="Shield Management"),
    BonusSpec("hull_upgrades", A.ARMOR_HP, 5.0, target_domain="ship",
              skill_id=SKILL_HULL_UPGRADES, per_level=True, label="Hull Upgrades"),
    BonusSpec("mechanics", A.HULL_HP, 5.0, target_domain="ship",
              skill_id=SKILL_MECHANICS, per_level=True, label="Mechanics"),
    BonusSpec("navigation", A.MAX_VELOCITY, 5.0, target_domain="ship",
              skill_id=SKILL_NAVIGATION, per_level=True, label="Navigation"),
    BonusSpec("cap_management", A.CAP_CAPACITY, 5.0, target_domain="ship",
              skill_id=SKILL_CAP_MANAGEMENT, per_level=True, label="Capacitor Management"),
    BonusSpec("cap_systems_op", A.CAP_RECHARGE_RATE, -5.0, target_domain="ship",
              skill_id=SKILL_CAP_SYSTEMS_OP, per_level=True, label="Capacitor Systems Operation"),
    # Fitting output: CPU Management / Power Grid Management raise the hull's own CPU / PG
    # output by 5% per level (a ship-domain bonus on the ship's cpuOutput / powerOutput).
    BonusSpec("cpu_management", A.CPU_OUTPUT, 5.0, target_domain="ship",
              skill_id=SKILL_CPU_MANAGEMENT, per_level=True, label="CPU Management"),
    BonusSpec("power_grid_management", A.POWER_OUTPUT, 5.0, target_domain="ship",
              skill_id=SKILL_POWER_GRID_MANAGEMENT, per_level=True, label="Power Grid Management"),
    # Fitting cost: Weapon Upgrades cuts a weapon's CPU need, Advanced Weapon Upgrades its
    # powergrid need. Both apply to turrets AND launchers, so each is matched by the two
    # weapon-defining effects (targetAttack / useMissiles) — one spec per effect.
    BonusSpec("weapon_upgrades_turret", A.CPU_USAGE, -5.0, skill_id=SKILL_WEAPON_UPGRADES,
              per_level=True, match_effect_id=A.EFFECT_TURRET, label="Weapon Upgrades"),
    BonusSpec("weapon_upgrades_launcher", A.CPU_USAGE, -5.0, skill_id=SKILL_WEAPON_UPGRADES,
              per_level=True, match_effect_id=A.EFFECT_LAUNCHER, label="Weapon Upgrades"),
    BonusSpec("adv_weapon_upgrades_turret", A.POWER_USAGE, -2.0, skill_id=SKILL_ADVANCED_WEAPON_UPGRADES,
              per_level=True, match_effect_id=A.EFFECT_TURRET, label="Advanced Weapon Upgrades"),
    BonusSpec("adv_weapon_upgrades_launcher", A.POWER_USAGE, -2.0, skill_id=SKILL_ADVANCED_WEAPON_UPGRADES,
              per_level=True, match_effect_id=A.EFFECT_LAUNCHER, label="Advanced Weapon Upgrades"),

    # --- Gunnery: rate-of-fire, base turret damage, specialisations -----------------------
    BonusSpec("gunnery", A.RATE_OF_FIRE, -2.0, skill_id=SKILL_GUNNERY, per_level=True,
              match_effect_id=A.EFFECT_TURRET, label="Gunnery"),
    BonusSpec("small_projectile", A.DAMAGE_MULTIPLIER, 5.0, skill_id=SKILL_SMALL_PROJECTILE,
              per_level=True, match_required_skill_id=SKILL_SMALL_PROJECTILE, label="Small Projectile Turret"),
    BonusSpec("medium_projectile", A.DAMAGE_MULTIPLIER, 5.0, skill_id=SKILL_MEDIUM_PROJECTILE,
              per_level=True, match_required_skill_id=SKILL_MEDIUM_PROJECTILE, label="Medium Projectile Turret"),
    BonusSpec("large_projectile", A.DAMAGE_MULTIPLIER, 5.0, skill_id=SKILL_LARGE_PROJECTILE,
              per_level=True, match_required_skill_id=SKILL_LARGE_PROJECTILE, label="Large Projectile Turret"),
    BonusSpec("medium_ac_spec", A.DAMAGE_MULTIPLIER, 2.0, skill_id=SKILL_MEDIUM_AC_SPEC,
              per_level=True, match_required_skill_id=SKILL_MEDIUM_AC_SPEC, label="Medium Autocannon Specialization"),

    # --- Drones (damage): Drone Interfacing scales all drones; size/racial scope by req-skill --
    BonusSpec("drone_interfacing", A.DRONE_DAMAGE_MULTIPLIER, 10.0, skill_id=SKILL_DRONE_INTERFACING,
              per_level=True, match_category_ids=(A.CATEGORY_DRONE,), label="Drone Interfacing"),
    BonusSpec("light_drone_op", A.DRONE_DAMAGE_MULTIPLIER, 5.0, skill_id=SKILL_LIGHT_DRONE_OP,
              per_level=True, match_required_skill_id=SKILL_LIGHT_DRONE_OP, label="Light Drone Operation"),
    BonusSpec("medium_drone_op", A.DRONE_DAMAGE_MULTIPLIER, 5.0, skill_id=SKILL_MEDIUM_DRONE_OP,
              per_level=True, match_required_skill_id=SKILL_MEDIUM_DRONE_OP, label="Medium Drone Operation"),
    BonusSpec("heavy_drone_op", A.DRONE_DAMAGE_MULTIPLIER, 5.0, skill_id=SKILL_HEAVY_DRONE_OP,
              per_level=True, match_required_skill_id=SKILL_HEAVY_DRONE_OP, label="Heavy Drone Operation"),
    BonusSpec("amarr_drone_spec", A.DRONE_DAMAGE_MULTIPLIER, 2.0, skill_id=SKILL_AMARR_DRONE_SPEC,
              per_level=True, match_required_skill_id=SKILL_AMARR_DRONE_SPEC, label="Amarr Drone Specialization"),
    BonusSpec("minmatar_drone_spec", A.DRONE_DAMAGE_MULTIPLIER, 2.0, skill_id=SKILL_MINMATAR_DRONE_SPEC,
              per_level=True, match_required_skill_id=SKILL_MINMATAR_DRONE_SPEC, label="Minmatar Drone Specialization"),
    BonusSpec("gallente_drone_spec", A.DRONE_DAMAGE_MULTIPLIER, 2.0, skill_id=SKILL_GALLENTE_DRONE_SPEC,
              per_level=True, match_required_skill_id=SKILL_GALLENTE_DRONE_SPEC, label="Gallente Drone Specialization"),
    BonusSpec("caldari_drone_spec", A.DRONE_DAMAGE_MULTIPLIER, 2.0, skill_id=SKILL_CALDARI_DRONE_SPEC,
              per_level=True, match_required_skill_id=SKILL_CALDARI_DRONE_SPEC, label="Caldari Drone Specialization"),
    # Missiles: damage skills modify the CHARGE (one spec per damage type, scoped by the charge's
    # required skill); Rapid Launch cuts launcher rate-of-fire.
    *tuple(
        BonusSpec(f"heavy_missiles_{d}", attr, 5.0, skill_id=SKILL_HEAVY_MISSILES, per_level=True,
                  target_domain="charge", match_required_skill_id=SKILL_HEAVY_MISSILES, label="Heavy Missiles")
        for d, attr in A.CHARGE_DAMAGE.items()
    ),
    *tuple(
        BonusSpec(f"warhead_upgrades_{d}", attr, 2.0, skill_id=SKILL_WARHEAD_UPGRADES, per_level=True,
                  target_domain="charge", match_required_skill_id=SKILL_MISSILE_LAUNCHER_OP, label="Warhead Upgrades")
        for d, attr in A.CHARGE_DAMAGE.items()
    ),
    BonusSpec("rapid_launch", A.RATE_OF_FIRE, -3.0, skill_id=SKILL_RAPID_LAUNCH, per_level=True,
              match_effect_id=A.EFFECT_LAUNCHER, label="Rapid Launch"),

    # --- Navigation / targeting / shield-fitting ------------------------------------------
    BonusSpec("evasive_maneuvering", A.AGILITY, -5.0, target_domain="ship",
              skill_id=SKILL_EVASIVE_MANEUVERING, per_level=True, label="Evasive Maneuvering"),
    BonusSpec("spaceship_command", A.AGILITY, -2.0, target_domain="ship",
              skill_id=SKILL_SPACESHIP_COMMAND, per_level=True, label="Spaceship Command"),
    BonusSpec("acceleration_control", A.SPEED_BONUS, 5.0, skill_id=SKILL_ACCELERATION_CONTROL,
              per_level=True, match_group_ids=PROP_GROUPS, label="Acceleration Control"),
    BonusSpec("long_range_targeting", A.MAX_TARGET_RANGE, 5.0, target_domain="ship",
              skill_id=SKILL_LONG_RANGE_TARGETING, per_level=True, label="Long Range Targeting"),
    BonusSpec("signature_analysis", A.SCAN_RESOLUTION, 5.0, target_domain="ship",
              skill_id=SKILL_SIGNATURE_ANALYSIS, per_level=True, label="Signature Analysis"),
    BonusSpec("shield_upgrades", A.POWER_USAGE, -5.0, skill_id=SKILL_SHIELD_UPGRADES,
              per_level=True, match_required_skill_id=SKILL_SHIELD_UPGRADES, label="Shield Upgrades"),
)


@dataclass
class BonusContext:
    """All bonuses in play for one evaluation: ship/role (data-provided) + skills."""
    ship_bonuses: list[BonusSpec] = field(default_factory=list)
    skill_bonuses: list[BonusSpec] = field(default_factory=lambda: list(STANDARD_SKILL_BONUSES))

    def all(self) -> list[BonusSpec]:
        return [*self.ship_bonuses, *self.skill_bonuses]
