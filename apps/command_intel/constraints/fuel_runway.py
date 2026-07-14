"""``fuel_runway`` — days until the soonest structure goes offline (design doc 05 §4).

Binding metric is the minimum ``fuel_days_left`` across the low-fuel structures the
infrastructure slice flags; the limiting factor is that structure. Severity comes
straight from the configured day-bands (critical 3 / high 7 / watch 14); demand is
the watch level, so headroom reads "days below the comfortable margin".
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

_DEFAULTS = {"critical_days": 3, "high_days": 7, "watch_days": 14}

# Seam B (``..messages``): persisted prose = scaffold key + JSON-safe params; the English column is
# derived from the msgid itself so the two cannot drift.
_LABEL = "constraint.fuel_runway.label"
_DETAIL = "constraint.fuel_runway.detail"
_DETAIL_NONE = "constraint.fuel_runway.detail.no_structures"
_DETAIL_UNKNOWN = "constraint.fuel_runway.detail.unknown"
_DETAIL_UNKNOWN_METRIC = "constraint.fuel_runway.detail.unknown_metric"


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
                key=self.key, category=self.category,
                label=messages.english(_LABEL), label_key=_LABEL,
                status=UNKNOWN, severity="info",
                detail=messages.english(_DETAIL_UNKNOWN), detail_key=_DETAIL_UNKNOWN,
            )]
        # No low-fuel structures flagged is a real, healthy answer — not unknown.
        if not structures:
            return [Constraint(
                key=self.key, category=self.category,
                label=messages.english(_LABEL), label_key=_LABEL, unit="days",
                severity="info",
                detail=messages.english(_DETAIL_NONE), detail_key=_DETAIL_NONE,
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
                key=self.key, category=self.category,
                label=messages.english(_LABEL), label_key=_LABEL,
                status=UNKNOWN, severity="info", inputs=inputs,
                detail=messages.english(_DETAIL_UNKNOWN_METRIC),
                detail_key=_DETAIL_UNKNOWN_METRIC,
            )]

        binding_in = min(computable, key=lambda i: i.value)
        binding = binding_in.value
        headroom = binding - watch
        detail_params = {
            "structure": binding_in.name, "days": f"{binding:g}",
            "count": len(computable), "watch": f"{watch:g}",
        }
        return [Constraint(
            key=self.key, category=self.category,
            label=messages.english(_LABEL), label_key=_LABEL,
            binding_metric=binding, unit="days", limiting_factor="fuel_days_left",
            headroom=headroom, score=constraint_score(headroom, watch),
            severity=band_severity(binding, crit, high, watch),
            affected_capabilities=[f"structure:{binding_in.name}"], inputs=inputs,
            detail=messages.english(_DETAIL, detail_params),
            detail_key=_DETAIL, detail_params=detail_params,
        )]


register_constraint(FuelRunwayProvider())
