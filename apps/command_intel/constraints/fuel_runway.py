"""``fuel_runway`` — days until the soonest structure goes offline (design doc 05 §4).

Binding metric is the minimum ``fuel_days_left`` across the low-fuel structures the
infrastructure slice flags; the limiting factor is that structure. Severity comes
straight from the configured day-bands (critical 3 / high 7 / watch 14); demand is
the watch level, so headroom reads "days below the comfortable margin".
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

_DEFAULTS = {"critical_days": 3, "high_days": 7, "watch_days": 14}


class FuelRunwayProvider:
    key = "fuel_runway"
    label = "Structure Fuel Runway"
    category = "infrastructure"
    default_enabled = True

    def compute(self, snapshot: dict, cfg: dict) -> list[Constraint]:
        infra = slice_facts(snapshot, "infrastructure")
        structures = infra.get("low_fuel_structures")
        pcfg = {**_DEFAULTS, **(cfg or {}).get("providers", {}).get(self.key, {})}
        crit, high, watch = pcfg["critical_days"], pcfg["high_days"], pcfg["watch_days"]

        if structures is None:
            return [Constraint(
                key=self.key, category=self.category, label=self.label, status=UNKNOWN,
                severity="info",
                detail="Cannot compute fuel runway: infrastructure slice has no structure data.",
            )]
        # No low-fuel structures flagged is a real, healthy answer — not unknown.
        if not structures:
            return [Constraint(
                key=self.key, category=self.category, label=self.label, unit="days",
                severity="info", detail="No structures are running low on fuel.",
            )]

        inputs = [
            LimitInput(
                s.get("name") or "structure", num(s.get("fuel_days_left")), "days",
                {"source": "infrastructure", "path": "low_fuel_structures[].fuel_days_left"},
            )
            for s in structures if isinstance(s, dict)
        ]
        computable = [i for i in inputs if i.value is not None]
        if not computable:
            return [Constraint(
                key=self.key, category=self.category, label=self.label, status=UNKNOWN,
                severity="info", inputs=inputs,
                detail="Cannot compute fuel runway: no structure reported fuel_days_left.",
            )]

        binding_in = min(computable, key=lambda i: i.value)
        binding = binding_in.value
        headroom = binding - watch
        return [Constraint(
            key=self.key, category=self.category, label=self.label,
            binding_metric=binding, unit="days", limiting_factor="fuel_days_left",
            headroom=headroom, score=constraint_score(headroom, watch),
            severity=band_severity(binding, crit, high, watch),
            affected_capabilities=[f"structure:{binding_in.name}"], inputs=inputs,
            detail=(
                f"{binding_in.name} runs out of fuel in {binding:g} day(s) — the soonest of "
                f"{len(computable)} low-fuel structure(s); watch margin is {watch:g} days."
            ),
        )]


register_constraint(FuelRunwayProvider())
