"""EVE stacking-penalty mathematics.

When several modules modify the *same* attribute multiplicatively, EVE reduces
the effectiveness of each successive module after the strongest. The published
per-module effectiveness (1.00, 0.869, 0.571, 0.283, 0.106, 0.030, …) is
reproduced by

    S(i) = exp(-(i / 2.67) ** 2)          # i = 0 for the strongest module

This module derives the combination from that documented formula alone; it does
not use any external engine's constants, tables or code.

Terminology:
* A *multiplier* ``m`` expresses a modified effect as a factor on the base value
  (``m = 1.20`` is "+20%", ``m = 0.70`` is "-30%"). The penalty acts on the
  fractional part ``(m - 1)``.
* Modules in the *same* stacking group are ranked by how far their multiplier is
  from 1.0 (strongest first) before the penalty is applied — exactly as the game
  ranks them, so the ordering of the caller's list never changes the result.
"""
from __future__ import annotations

import math

# The single documented constant. See module docstring for the reproduced table.
_PENALTY_C = 2.67


def penalty_factor(index: int) -> float:
    """Effectiveness ``S(i)`` of the ``index``-th strongest module (0-based)."""
    if index < 0:
        raise ValueError("stacking index must be >= 0")
    return math.exp(-((index / _PENALTY_C) ** 2))


def combine_penalized(multipliers: list[float]) -> float:
    """Combine same-group multiplicative modifiers with the stacking penalty.

    Returns the single factor to multiply the base attribute by. The strongest
    modifier (largest ``|m - 1|``) keeps full effect; each weaker one is scaled by
    :func:`penalty_factor`. An empty list is a no-op (returns ``1.0``).
    """
    if not multipliers:
        return 1.0
    # Rank by distance from 1.0, strongest first — the game's own ordering.
    ordered = sorted(multipliers, key=lambda m: abs(m - 1.0), reverse=True)
    factor = 1.0
    for i, m in enumerate(ordered):
        factor *= 1.0 + (m - 1.0) * penalty_factor(i)
    return factor


def combine_unpenalized(multipliers: list[float]) -> float:
    """Plain product of multipliers (used for effects that do NOT stack-penalise,
    e.g. skill and ship/role bonuses, which apply at full strength)."""
    factor = 1.0
    for m in multipliers:
        factor *= m
    return factor
