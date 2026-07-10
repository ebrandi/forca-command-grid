"""Objective progress math (design doc 04 §3.1) — pure functions, no DB.

One computation path for both directions, clamped to integers in [0, 100], unit-tested for
gte/lte, NULL handling, and the divide-by-zero degenerate cases. ``services.py`` reads the
stored ``baseline``/``target``/``current`` off an objective and calls
:func:`objective_progress_value`; keeping the arithmetic here means the campaign-progress
aggregation and the health rules can never disagree with the single per-objective rule.
"""
from __future__ import annotations

from decimal import Decimal

GTE = "gte"
LTE = "lte"


def _clamp(value: Decimal, lo: int, hi: int) -> int:
    """Clamp ``value`` into ``[lo, hi]`` as an integer (truncating toward zero for positives —
    i.e. floor, so a target is only ever shown as reached at exactly 100)."""
    if value <= lo:
        return lo
    if value >= hi:
        return hi
    return int(value)


def objective_progress_value(
    baseline: Decimal | None,
    target: Decimal | None,
    current: Decimal | None,
    direction: str,
) -> int:
    """Progress percent for one objective (doc 04 §3.1).

    * ``current`` NULL (never measured) → 0 (the objective is "unmeasured").
    * ``gte`` (reach at least ``target``): ``clamp((current − baseline)/(target − baseline)×100)``
      with NULL baseline treated as 0; degenerate ``target == baseline`` → 100 iff ``current ≥
      target`` else 0.
    * ``lte`` (keep at or below ``target``): 100 when ``current ≤ target``; otherwise the
      proportional distance from the baseline ``clamp((baseline − current)/(baseline − target)
      ×100, 0, 99)`` when ``baseline > target``, capped at 99 so only a genuinely met objective
      reads 100; NULL/degenerate baseline with ``current > target`` → 0.
    """
    if current is None:
        return 0

    if direction == LTE:
        if target is None:
            return 0
        if current <= target:
            return 100
        if baseline is not None and baseline > target:
            return _clamp((baseline - current) / (baseline - target) * Decimal(100), 0, 99)
        return 0

    # Default / gte.
    base = baseline if baseline is not None else Decimal(0)
    if target is None:
        return 0
    if target == base:
        return 100 if current >= target else 0
    return _clamp((current - base) / (target - base) * Decimal(100), 0, 100)
