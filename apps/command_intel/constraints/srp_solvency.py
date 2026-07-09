"""``srp_solvency`` — months the SRP programme can sustain payouts (design doc 05 §4).

Binding metric is ``(budget − open_liability) ÷ spent_period`` months of cover at the
current burn. The lever an action relieves is the SRP budget (``limiting_factor =
srp_budget``). Severity comes from the configured month-bands (critical 0.5 / high 1
/ watch 2); demand is one month of cover.
"""
from __future__ import annotations

from ..engine.base import (
    UNKNOWN,
    Constraint,
    LimitInput,
    constraint_score,
)
from ..engine.registry import register_constraint
from ._common import band_severity, num, slice_facts

_DEFAULTS = {"critical_months": 0.5, "high_months": 1, "watch_months": 2}


class SrpSolvencyProvider:
    key = "srp_solvency"
    label = "SRP Solvency"
    category = "financial"
    default_enabled = True

    def compute(self, snapshot: dict, cfg: dict) -> list[Constraint]:
        srp = slice_facts(snapshot, "srp")
        pcfg = {**_DEFAULTS, **(cfg or {}).get("providers", {}).get(self.key, {})}
        crit, high, watch = pcfg["critical_months"], pcfg["high_months"], pcfg["watch_months"]

        budget = num(srp.get("budget_isk"))
        liability = num(srp.get("open_liability_isk"))
        spent = num(srp.get("spent_period_isk"))
        inputs = [
            LimitInput("budget_isk", budget, "ISK", {"source": "srp", "path": "budget_isk"}),
            LimitInput("open_liability_isk", liability, "ISK",
                       {"source": "srp", "path": "open_liability_isk"}),
            LimitInput("spent_period_isk", spent, "ISK", {"source": "srp", "path": "spent_period_isk"}),
        ]
        if budget is None or liability is None or spent is None:
            return [Constraint(
                key=self.key, category=self.category, label=self.label, status=UNKNOWN,
                severity="info", affected_capabilities=["program:srp"], inputs=inputs,
                detail="Cannot compute SRP solvency: budget, open liability or period spend missing.",
            )]

        months = round((budget - liability) / max(spent, 1), 2)
        demand = high  # one month of cover is the target
        headroom = round(months - demand, 2)
        return [Constraint(
            key=self.key, category=self.category, label=self.label,
            binding_metric=months, unit="months", limiting_factor="srp_budget",
            headroom=headroom, score=constraint_score(headroom, demand),
            severity=band_severity(months, crit, high, watch),
            affected_capabilities=["program:srp"], inputs=inputs,
            detail=(
                f"SRP can sustain {months:g} month(s) of payouts: "
                f"({budget:,.0f} budget − {liability:,.0f} open liability) ÷ {spent:,.0f} period spend; "
                f"target is {demand:g} month(s)."
            ),
        )]


register_constraint(SrpSolvencyProvider())
