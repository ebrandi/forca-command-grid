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
SKILL_WARHEAD_UPGRADES = 3317     # +2% missile (kinetic/thermal/em/explosive) damage / level

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
    BonusSpec("surgical_strike", A.DAMAGE_MULTIPLIER, 3.0, skill_id=SKILL_SURGICAL_STRIKE,
              per_level=True, match_attr_present=A.DAMAGE_MULTIPLIER, label="Surgical Strike"),
    # Missiles take all damage from the charge (launchers have no damageMultiplier attr), so
    # match every launcher by its useMissiles effect — robust across cruise/rapid/XL/… groups.
    BonusSpec("warhead_upgrades", A.DAMAGE_MULTIPLIER, 2.0, skill_id=SKILL_WARHEAD_UPGRADES,
              per_level=True, match_effect_id=A.EFFECT_LAUNCHER, label="Warhead Upgrades"),
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
)


@dataclass
class BonusContext:
    """All bonuses in play for one evaluation: ship/role (data-provided) + skills."""
    ship_bonuses: list[BonusSpec] = field(default_factory=list)
    skill_bonuses: list[BonusSpec] = field(default_factory=lambda: list(STANDARD_SKILL_BONUSES))

    def all(self) -> list[BonusSpec]:
        return [*self.ship_bonuses, *self.skill_bonuses]
