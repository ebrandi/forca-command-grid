"""``srp_solvency`` — months the SRP programme can sustain payouts (design doc 05 §4).

Binding metric is ``(budget − open_liability) ÷ spent_period`` months of cover at the
current burn. The lever an action relieves is the SRP budget (``limiting_factor =
srp_budget``). Severity comes from the configured month-bands (critical 0.5 / high 1
/ watch 2); demand is one month of cover.
"""
from __future__ import annotations

from .. import messages
from ..engine.base import (
    UNKNOWN,
    Constraint,
    LimitInput,
    constraint_score,
)
from ..engine.registry import register_constraint
from ._common import band_severity, num, slice_facts

_DEFAULTS = {"critical_months": 0.5, "high_months": 1, "watch_months": 2}

# Seam B (``..messages``): persisted prose = scaffold key + JSON-safe params; the English column is
# derived from the msgid itself so the two cannot drift.
_LABEL = "constraint.srp_solvency.label"
_DETAIL = "constraint.srp_solvency.detail"
_DETAIL_UNKNOWN = "constraint.srp_solvency.detail.unknown"


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
                key=self.key, category=self.category,
                label=messages.english(_LABEL), label_key=_LABEL,
                status=UNKNOWN, severity="info",
                affected_capabilities=["program:srp"], inputs=inputs,
                detail=messages.english(_DETAIL_UNKNOWN), detail_key=_DETAIL_UNKNOWN,
            )]

        months = round((budget - liability) / max(spent, 1), 2)
        demand = high  # one month of cover is the target
        headroom = round(months - demand, 2)
        detail_params = {
            "months": f"{months:g}", "budget": f"{budget:,.0f}",
            "liability": f"{liability:,.0f}", "spent": f"{spent:,.0f}",
            "target": f"{demand:g}",
        }
        return [Constraint(
            key=self.key, category=self.category,
            label=messages.english(_LABEL), label_key=_LABEL,
            binding_metric=months, unit="months", limiting_factor="srp_budget",
            headroom=headroom, score=constraint_score(headroom, demand),
            severity=band_severity(months, crit, high, watch),
            affected_capabilities=["program:srp"], inputs=inputs,
            detail=messages.english(_DETAIL, detail_params),
            detail_key=_DETAIL, detail_params=detail_params,
        )]


register_constraint(SrpSolvencyProvider())
