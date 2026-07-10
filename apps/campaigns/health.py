"""Campaign health rule evaluation (design doc 00 §4) — a pure function over assembled facts.

``services.campaign_health`` gathers the facts (objective/risk/issue/dependency counts, dates,
budget ratio) with a handful of indexed queries and hands them here; this module contains no
DB access so the whole rule set is deterministic and unit-testable in isolation. Each triggered
rule appends a ``{code, label, detail}`` reason; the overall health is the **worst** triggered
level (blocked ≻ critical ≻ at_risk ≻ watch ≻ healthy), with ``unknown`` as a short-circuit when
there is nothing meaningful to measure yet.

Reason **labels come from this code-side catalogue** — only ``detail`` carries data — so the
health panel never renders attacker-influenced markup (threat T5).
"""
from __future__ import annotations

# Worst-wins ordering. ``unknown`` is handled as a short-circuit, not ranked here.
_RANK = {"healthy": 0, "watch": 1, "at_risk": 2, "critical": 3, "blocked": 4}

# code -> (level, fixed human label). detail is supplied per-trigger and is the only data part.
_CATALOGUE: dict[str, tuple[str, str]] = {
    "mandatory_blocked": ("blocked", "A mandatory objective is blocked"),
    "dependency_blocked": ("blocked", "A mandatory objective is blocked by an unresolved dependency"),
    "past_deadline": ("critical", "Past the target date with work outstanding"),
    "budget_overrun": ("critical", "Budget overrun"),
    "risk_critical": ("critical", "A critical risk is overdue"),
    "deadline_shortfall": ("at_risk", "Behind the expected pace"),
    "overdue_objectives": ("at_risk", "Objectives are overdue"),
    "escalated_issues": ("at_risk", "Issues are escalated"),
    "budget_warning": ("at_risk", "Budget nearly exhausted"),
    "blocked_objectives": ("at_risk", "Non-mandatory objectives are blocked"),
    "dependency_blocker": ("at_risk", "An unresolved dependency is blocking progress"),
    "stale_metrics": ("watch", "Automatic metrics are stale"),
    "unowned_objectives": ("watch", "Objectives have no owner"),
    "inactive": ("watch", "No recent activity"),
}


def _reason(code: str, detail: str = "") -> dict:
    level, label = _CATALOGUE[code]
    return {"code": code, "label": label, "detail": detail}


def evaluate(facts: dict, thresholds: dict) -> tuple[str, list[dict]]:
    """Return ``(health_state, reasons)`` for a campaign given assembled ``facts``.

    ``unknown`` short-circuits: a campaign that is not active, or has nothing measurable yet,
    has no honest health signal.
    """
    if facts.get("not_active"):
        return "unknown", [{"code": "not_active", "label": "Campaign is not active", "detail": ""}]
    if facts.get("no_measurable"):
        return "unknown", [{"code": "no_measurable", "label": "No measurable objectives yet",
                            "detail": ""}]

    reasons: list[dict] = []

    # --- blocked -------------------------------------------------------------
    if facts.get("mandatory_blocked"):
        reasons.append(_reason("mandatory_blocked"))
    if facts.get("dep_blocked_mandatory"):
        reasons.append(_reason("dependency_blocked"))

    # --- critical ------------------------------------------------------------
    if facts.get("past_deadline"):
        reasons.append(_reason("past_deadline"))
    ratio = facts.get("budget_ratio")
    # The exact spend ratio is derived from budget/spent (director + commander only, doc 07 §1.5),
    # but a health reason renders on plain can_view surfaces — so the label carries the signal and
    # the percentage is never embedded in ``detail`` (#7).
    if ratio is not None and ratio > thresholds["budget_critical_ratio"]:
        reasons.append(_reason("budget_overrun"))
    if facts.get("risk_sev9_overdue"):
        reasons.append(_reason("risk_critical"))

    # --- at_risk -------------------------------------------------------------
    shortfall = facts.get("deadline_shortfall")
    if shortfall is not None and shortfall > thresholds["deadline_shortfall_pts"]:
        reasons.append(_reason("deadline_shortfall", f"{shortfall} points behind pace"))
    overdue = facts.get("overdue_objectives", 0)
    if overdue:
        reasons.append(_reason("overdue_objectives", f"{overdue} overdue"))
    escalated = facts.get("escalated_issues", 0)
    if escalated:
        reasons.append(_reason("escalated_issues", f"{escalated} escalated"))
    # Budget "warning" only when it is not already an overrun (avoid double-counting).
    if (ratio is not None and ratio >= thresholds["budget_warn_ratio"]
            and ratio <= thresholds["budget_critical_ratio"]):
        reasons.append(_reason("budget_warning"))
    blocked_nonmandatory = facts.get("blocked_nonmandatory", 0)
    if blocked_nonmandatory:
        reasons.append(_reason("blocked_objectives", f"{blocked_nonmandatory} blocked"))
    if facts.get("dep_blocker_any"):
        reasons.append(_reason("dependency_blocker"))

    # --- watch ---------------------------------------------------------------
    stale = facts.get("stale_metrics", 0)
    if stale:
        reasons.append(_reason("stale_metrics", f"{stale} stale"))
    unowned = facts.get("unowned_objectives", 0)
    if unowned:
        reasons.append(_reason("unowned_objectives", f"{unowned} without an owner"))
    if facts.get("inactive"):
        reasons.append(_reason("inactive"))

    if not reasons:
        return "healthy", []
    worst = max(reasons, key=lambda r: _RANK[_CATALOGUE[r["code"]][0]])
    return _CATALOGUE[worst["code"]][0], reasons
