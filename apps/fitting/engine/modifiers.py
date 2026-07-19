"""Generic dogma-graph → BonusSpec applicator (Tocha's Lab Phase 2).

Translates imported CCP **skill dogma** + the **SdeModifier** graph (Phase 1) into the engine's
existing :class:`~apps.fitting.engine.bonuses.BonusSpec` representation, so *every* trained skill
that carries a ``postPercent`` modifier is applied — not just the ~45 hand-coded in
``bonuses.py``. The output feeds the same, already-tested stacking / domain / matching machinery
the formula layer uses; nothing about the evaluation maths changes.

**Merge, not replace.** ``STANDARD_SKILL_BONUSES`` stays authoritative for the skills it already
covers (each is locked by a golden test, and a few — e.g. Heavy Missiles, the drone-size skills —
have *no* ``postPercent`` modifier in the graph, so translating alone would silently drop them).
This module therefore only produces specs for skills **not** already hand-coded, and the caller
appends them to the hand-coded set. That closes the identified gap ("any skill I haven't
hand-entered isn't applied") with zero regression on the validated fits.

Only ``postPercent`` (operation 6) modifiers are translated — the one percentage form the
multiplicative ``BonusSpec`` model represents. Additive/assignment and the skill-infrastructure
modifiers (skillLevel/skillPoints, operations 0/2/9) are not combat bonuses and are skipped.
The func → domain/target mapping mirrors ``import_ship_bonuses._row_from_modifier`` exactly, so
skills and hull bonuses are classified identically.
"""
from __future__ import annotations

from typing import Protocol

from . import attributes as A
from .bonuses import BonusSpec
from .effects import Op

_OP_POSTPERCENT = 6
# em/explosive/kinetic/thermal charge-damage attrs — a bonus on one of these lands on the loaded
# CHARGE (the missile), so it is scoped to the charge domain, not the launcher (see Warhead
# Upgrades: OwnerRequiredSkillModifier op6 on 114/116/117/118).
_CHARGE_DAMAGE_ATTRS = frozenset(A.CHARGE_DAMAGE.values())


class _Modifier(Protocol):
    """The shape the translator needs from an ``SdeModifier`` row (duck-typed for testability)."""
    func: str
    operation: int | None
    modified_attribute_id: int | None
    modifying_attribute_id: int | None
    group_id: int | None
    skill_type_id: int | None


def spec_from_skill_modifier(
    skill_id: int, level_value: float | None, m: _Modifier, label: str
) -> BonusSpec | None:
    """One graph modifier of a skill → a per-level :class:`BonusSpec`, or ``None`` if not
    representable. ``level_value`` is the skill's own dogma value for the modifier's *modifying*
    attribute (the per-level percentage, e.g. -3 for Rapid Launch's ``rofBonus``)."""
    if m.operation != _OP_POSTPERCENT:
        return None
    target_attr = m.modified_attribute_id
    if target_attr is None or not level_value:
        return None
    kw: dict = {
        "target_attr": int(target_attr),
        "amount": float(level_value),
        "skill_id": int(skill_id),
        "per_level": True,
        "penalised": False,
        "op": Op.MULTIPLY,
        "label": label[:128],
    }
    func = m.func
    scope = m.group_id if m.group_id is not None else m.skill_type_id
    if func in ("ItemModifier", "LocationModifier"):
        # A skill's ship-domain bonus (CPU Management → ship cpuOutput; Shield Management →
        # shieldCapacity; Evasive Maneuvering → agility). Character-attribute skills (domain
        # charID) target attrs the formula layer never reads, so they are harmless no-ops.
        kw["target_domain"] = "ship"
    elif func == "LocationGroupModifier":
        if m.group_id is None:
            return None
        kw["target_domain"] = "item"
        kw["match_group_ids"] = (int(m.group_id),)
    elif func in ("LocationRequiredSkillModifier", "OwnerRequiredSkillModifier"):
        if m.skill_type_id is None:
            return None
        kw["match_required_skill_id"] = int(m.skill_type_id)
        kw["target_domain"] = "charge" if int(target_attr) in _CHARGE_DAMAGE_ATTRS else "item"
    else:
        return None                      # unknown func — skip, never over-apply
    key = f"gs_{skill_id}_{func}_{target_attr}_{scope or 0}"
    return BonusSpec(key=key, **kw)


def build_skill_bonus_specs(
    skill_attrs: dict[int, dict[int, float]],
    effect_ids_by_skill: dict[int, list[int]],
    modifiers_by_effect: dict[int, list[_Modifier]],
    labels: dict[int, str],
    exclude_skill_ids: frozenset[int],
) -> list[BonusSpec]:
    """Build the data-driven skill catalogue.

    ``skill_attrs`` maps skill type-id → its dogma attribute values; ``effect_ids_by_skill`` maps
    skill → its effect ids; ``modifiers_by_effect`` maps effect id → its (postPercent) modifiers.
    Skills in ``exclude_skill_ids`` are already hand-coded and are skipped so nothing is applied
    twice or lost. Deterministic (sorted by skill id) so the cache key and traces are stable.
    """
    specs: list[BonusSpec] = []
    for skill_id in sorted(skill_attrs):
        if skill_id in exclude_skill_ids:
            continue
        attrs = skill_attrs[skill_id]
        label = labels.get(skill_id, f"Skill {skill_id}")
        seen: set[str] = set()
        for eid in effect_ids_by_skill.get(skill_id, ()):
            for m in modifiers_by_effect.get(eid, ()):
                level_value = attrs.get(m.modifying_attribute_id)
                spec = spec_from_skill_modifier(skill_id, level_value, m, label)
                if spec is None or spec.key in seen:
                    continue
                seen.add(spec.key)
                specs.append(spec)
    return specs
