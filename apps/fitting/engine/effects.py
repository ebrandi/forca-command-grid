"""Modifier combination semantics.

The v2 graph evaluator applies dogma effects generically from the imported CCP modifier
graph (see :mod:`apps.fitting.engine.graph`), so this module no longer carries a curated
handler table. What remains is the small, shared :class:`Op` enum describing how a computed
modifier combines with its target attribute's running value — used by the bonus
specification (:mod:`apps.fitting.engine.bonuses`) and the ORM provider's hull-bonus specs.
"""
from __future__ import annotations

from enum import Enum


class Op(str, Enum):
    """How a modifier combines with the target attribute's running value."""
    MULTIPLY = "multiply"        # value *= factor            (stack-penalised group)
    ADD = "add"                  # value += amount            (never penalised)
    FORCE = "force"              # value = amount             (rare; e.g. mode overrides)
