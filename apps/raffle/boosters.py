"""Activity safeguard + prize-value booster status (pure read helpers).

Two leadership levers, both driven by an :mod:`apps.raffle.metrics` metric:

* **Minimum activity** — the automatic draw is *held* until the contest reaches the
  configured metric threshold, so prizes aren't handed out for a dead event.
  Leadership can still force a manual draw.
* **Prize-value booster** — once a (usually higher) goal is reached, ISK/PLEX prize
  values are boosted by a configured percentage.

Imports only ``metrics`` + models, so both ``services`` and ``draw`` can use it
without an import cycle.
"""
from __future__ import annotations

from decimal import Decimal

from . import metrics

BOOSTABLE_PRIZE_TYPES = {"isk", "plex"}


def _pct(value: Decimal, target: Decimal) -> int:
    if target <= 0:
        return 100
    return min(100, int(value / target * 100))


def min_activity_status(contest, *, value: Decimal | None = None) -> dict:
    """Whether the contest has reached the minimum activity for a valid draw."""
    key = contest.min_activity_metric
    threshold = Decimal(contest.min_activity_threshold or 0)
    if not key or threshold <= 0:
        return {"configured": False, "met": True, "metric": "", "threshold": Decimal("0")}
    if value is None:
        value = metrics.value_of(contest, key)
    return {
        "configured": True,
        "metric": key,
        "label": metrics.label(key),
        "unit": metrics.unit(key),
        "money": metrics.is_money(key),
        "threshold": threshold,
        "value": value,
        "met": value >= threshold,
        "pct": _pct(value, threshold),
        "remaining": max(Decimal("0"), threshold - value),
    }


def prize_booster_status(contest, *, value: Decimal | None = None) -> dict:
    """Whether the prize-value booster goal has been reached, and by how much."""
    key = contest.prize_booster_metric
    goal = Decimal(contest.prize_booster_goal or 0)
    percent = Decimal(contest.prize_booster_percent or 0)
    if not key or goal <= 0 or percent <= 0:
        return {"configured": False, "achieved": False, "percent": Decimal("0"), "metric": ""}
    if value is None:
        value = metrics.value_of(contest, key)
    return {
        "configured": True,
        "metric": key,
        "label": metrics.label(key),
        "unit": metrics.unit(key),
        "money": metrics.is_money(key),
        "goal": goal,
        "value": value,
        "percent": percent,
        "achieved": value >= goal,
        "pct": _pct(value, goal),
        "remaining": max(Decimal("0"), goal - value),
    }


def prize_multiplier(contest, *, achieved: bool | None = None) -> Decimal:
    """The ISK/PLEX prize multiplier (1 unless the booster goal is achieved)."""
    if achieved is None:
        achieved = prize_booster_status(contest)["achieved"]
    if not achieved:
        return Decimal("1")
    return Decimal("1") + Decimal(contest.prize_booster_percent or 0) / Decimal("100")


def is_boostable(prize, contest, *, kind: str = "ticket") -> bool:
    """Whether this prize is in scope for the booster at all.

    Two independent gates. The prize TYPE gate is unchanged: only ISK and PLEX are
    fungible and unit-priced, so only they can be scaled by a percentage — "10% more
    Rifter" is not a thing. The prize KIND gate is the leadership choice: a contest may
    boost only its ticket prizes, only its rank prizes, or both.
    """
    if prize.prize_type not in BOOSTABLE_PRIZE_TYPES:
        return False
    scope = getattr(contest, "prize_booster_applies_to", "both") or "both"
    return scope in ("both", kind)


def effective_prize_value(prize, contest, *, achieved: bool | None = None,
                          kind: str = "ticket") -> Decimal:
    """A prize's value after the prize-value booster (ISK/PLEX, and in-scope kinds only)."""
    base = Decimal(prize.estimated_value or 0)
    if not is_boostable(prize, contest, kind=kind):
        return base
    return (base * prize_multiplier(contest, achieved=achieved)).quantize(Decimal("1"))
