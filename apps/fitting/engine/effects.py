"""Documented module-effect handlers.

Rather than a full generic dogma virtual machine (which the current data layer does
not carry the modifier graph for), the engine applies a small, explicit and *tested*
set of effect handlers derived from publicly documented EVE mechanics. Each handler
declares which base attribute it reads on the module and which ship attribute it
modifies, and whether the modifier stacks with the penalty.

An effect the engine does not recognise is never silently dropped: the evaluator adds
it to ``FittingResult.unsupported`` so the UI can say so honestly. New mechanics are
added here (the documented extension point), not scattered across the codebase.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from . import attributes as A


class Op(str, Enum):
    """How a modifier combines with the target attribute's running value."""
    MULTIPLY = "multiply"        # value *= factor            (stack-penalised group)
    ADD = "add"                  # value += amount            (never penalised)
    FORCE = "force"              # value = amount             (rare; e.g. mode overrides)


@dataclass(frozen=True)
class ModuleModifier:
    """A normalised, engine-ready modifier produced from a module's attributes."""
    target_domain: str           # "ship" | "self"
    target_attribute_id: int
    op: Op
    value: float
    stacking_group: str | None   # None => not penalised; else the group key
    label: str = ""


# Handlers keyed by a stable semantic name. Each maps the module's own dogma attribute
# (source_attr) to a modifier on the ship. ``factor_from`` converts the stored attribute
# value into the modifier value.
@dataclass(frozen=True)
class EffectHandler:
    name: str
    source_attr: int             # attribute read on the module
    target_attr: int             # attribute changed on the ship
    op: Op
    penalised: bool
    # factor kind: "percent_bonus" -> value% => *(1+value/100); "resonance" -> value is a
    # multiplier already; "flat" -> value added as-is.
    kind: str = "percent_bonus"


# The supported handler set. Deliberately conservative and documented; extend here.
SHIELD_EXTENDER = EffectHandler("shield_extender", A.SHIELD_HP, A.SHIELD_HP, Op.ADD, False, "flat")
ARMOR_PLATE = EffectHandler("armor_plate", A.ARMOR_HP, A.ARMOR_HP, Op.ADD, False, "flat")


def resonance_handlers() -> list[EffectHandler]:
    """Resistance modules multiply the ship's resonance for each damage type (lower is
    better). Penalised as a group per tank layer + damage type."""
    handlers: list[EffectHandler] = []
    for layer in (A.SHIELD_RESONANCE, A.ARMOR_RESONANCE, A.HULL_RESONANCE):
        for _dtype, attr in layer.items():
            handlers.append(EffectHandler(
                name=f"resonance_{attr}", source_attr=attr, target_attr=attr,
                op=Op.MULTIPLY, penalised=True, kind="resonance",
            ))
    return handlers


# Attributes that, when present on a module, act as a *self* multiplier on the module's
# own output (damage mult, rate of fire) and are governed by skills/ship bonuses rather
# than stacking penalties. Handled directly in the DPS path, not as ship modifiers.
SELF_OUTPUT_ATTRS = frozenset({A.DAMAGE_MULTIPLIER, A.RATE_OF_FIRE})
