"""``isk_runway`` — days of operating runway at the current burn (design doc 05 §4).

Burn is the 30-day net outflow per day (zero when cashflow is positive). Binding
metric is ``balance_isk ÷ burn`` days; with non-negative cashflow there is no runway
constraint, so it reports ``info`` rather than a fabricated number. Severity comes
from the configured day-bands (critical 30 / high 60 / watch 120); the lever an
action relieves is ``balance_isk``.
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

_DEFAULTS = {"critical_days": 30, "high_days": 60, "watch_days": 120}


class IskRunwayProvider:
    key = "isk_runway"
    label = "ISK Runway"
    category = "financial"
    default_enabled = True

    def compute(self, snapshot: dict, cfg: dict) -> list[Constraint]:
        fin = slice_facts(snapshot, "finance")
        pcfg = {**_DEFAULTS, **(cfg or {}).get("providers", {}).get(self.key, {})}
        crit, high, watch = pcfg["critical_days"], pcfg["high_days"], pcfg["watch_days"]

        balance = num(fin.get("balance_isk"))
        net_30d = num(fin.get("net_30d_isk"))
        inputs = [
            LimitInput("balance_isk", balance, "ISK", {"source": "finance", "path": "balance_isk"}),
            LimitInput("net_30d_isk", net_30d, "ISK", {"source": "finance", "path": "net_30d_isk"}),
        ]
        if balance is None or net_30d is None:
            return [Constraint(
                key=self.key, category=self.category, label=self.label, status=UNKNOWN,
                severity="info", affected_capabilities=["program:treasury"], inputs=inputs,
                detail="Cannot compute ISK runway: wallet balance or 30-day net missing.",
            )]

        burn = abs(net_30d) / 30 if net_30d < 0 else 0.0
        if burn <= 0:
            return [Constraint(
                key=self.key, category=self.category, label=self.label, unit="days",
                severity="info", limiting_factor="balance_isk",
                affected_capabilities=["program:treasury"], inputs=inputs,
                detail="30-day net cashflow is positive — no ISK runway constraint.",
            )]

        days = round(balance / burn, 1)
        demand = high  # the operating-runway target in days
        headroom = round(days - demand, 1)
        return [Constraint(
            key=self.key, category=self.category, label=self.label,
            binding_metric=days, unit="days", limiting_factor="balance_isk",
            headroom=headroom, score=constraint_score(headroom, demand),
            severity=band_severity(days, crit, high, watch),
            affected_capabilities=["program:treasury"], inputs=inputs,
            detail=(
                f"{days:g} days of runway at the current burn "
                f"({balance:,.0f} balance ÷ {burn:,.0f}/day net outflow); target is {demand:g} days."
            ),
        )]


register_constraint(IskRunwayProvider())
