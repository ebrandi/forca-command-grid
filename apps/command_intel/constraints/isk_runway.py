"""``isk_runway`` — days of operating runway at the current burn (design doc 05 §4).

Burn is the 30-day net outflow per day (zero when cashflow is positive). Binding
metric is ``balance_isk ÷ burn`` days; with non-negative cashflow there is no runway
constraint, so it reports ``info`` rather than a fabricated number. Severity comes
from the configured day-bands (critical 30 / high 60 / watch 120); the lever an
action relieves is ``balance_isk``.
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

_DEFAULTS = {"critical_days": 30, "high_days": 60, "watch_days": 120}

# Seam B (``..messages``): persisted prose = scaffold key + JSON-safe params; the English column is
# derived from the msgid itself so the two cannot drift.
_LABEL = "constraint.isk_runway.label"
_DETAIL = "constraint.isk_runway.detail"
_DETAIL_POSITIVE = "constraint.isk_runway.detail.positive"
_DETAIL_UNKNOWN = "constraint.isk_runway.detail.unknown"


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
                key=self.key, category=self.category,
                label=messages.english(_LABEL), label_key=_LABEL,
                status=UNKNOWN, severity="info",
                affected_capabilities=["program:treasury"], inputs=inputs,
                detail=messages.english(_DETAIL_UNKNOWN), detail_key=_DETAIL_UNKNOWN,
            )]

        burn = abs(net_30d) / 30 if net_30d < 0 else 0.0
        if burn <= 0:
            return [Constraint(
                key=self.key, category=self.category,
                label=messages.english(_LABEL), label_key=_LABEL, unit="days",
                severity="info", limiting_factor="balance_isk",
                affected_capabilities=["program:treasury"], inputs=inputs,
                detail=messages.english(_DETAIL_POSITIVE), detail_key=_DETAIL_POSITIVE,
            )]

        days = round(balance / burn, 1)
        demand = high  # the operating-runway target in days
        headroom = round(days - demand, 1)
        detail_params = {
            "days": f"{days:g}", "balance": f"{balance:,.0f}", "burn": f"{burn:,.0f}",
            "target": f"{demand:g}",
        }
        return [Constraint(
            key=self.key, category=self.category,
            label=messages.english(_LABEL), label_key=_LABEL,
            binding_metric=days, unit="days", limiting_factor="balance_isk",
            headroom=headroom, score=constraint_score(headroom, demand),
            severity=band_severity(days, crit, high, watch),
            affected_capabilities=["program:treasury"], inputs=inputs,
            detail=messages.english(_DETAIL, detail_params),
            detail_key=_DETAIL, detail_params=detail_params,
        )]


register_constraint(IskRunwayProvider())
