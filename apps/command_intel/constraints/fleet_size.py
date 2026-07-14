"""``fleet_size.<doctrine>`` — max pilots we can field per doctrine (design doc 05 §4).

Binding-metric (Liebig minimum) over the two facts the doctrine slice provides:
qualified pilots and staged hulls. One :class:`Constraint` per doctrine; demand is
seeded from data (leadership's choice) — an explicit ``demand_targets`` override,
else the doctrine's own ``min_pilots``. A doctrine missing either fact is reported
``unknown`` with the reason, never fabricated.
"""
from __future__ import annotations

from .. import messages
from ..engine.base import (
    PRIMARY,
    SECONDARY,
    UNKNOWN,
    Constraint,
    LimitInput,
    constraint_score,
    severity_for,
)
from ..engine.registry import register_constraint
from ._common import num, slice_facts

# Seam B (``..messages``): persisted prose = scaffold key + JSON-safe params; the English column is
# derived from the msgid itself so the two cannot drift.
_LABEL = "constraint.fleet_size.label"
_DETAIL = "constraint.fleet_size.detail"
_DETAIL_NO_DEMAND = "constraint.fleet_size.detail.no_demand"
_DETAIL_UNKNOWN = "constraint.fleet_size.detail.unknown"


class FleetSizeProvider:
    key = "fleet_size"
    label = "Fleet Size"
    category = "combat"
    default_enabled = True

    def compute(self, snapshot: dict, cfg: dict) -> list[Constraint]:
        doctrines = slice_facts(snapshot, "doctrine").get("doctrines") or []
        targets = (cfg or {}).get("demand_targets", {})
        out: list[Constraint] = []
        for d in doctrines:
            if not isinstance(d, dict):
                continue
            out.append(self._one(d, targets, cfg))
        return out

    def _one(self, d: dict, targets: dict, cfg: dict) -> Constraint:
        slug = d.get("slug") or d.get("name") or "?"
        name = d.get("name") or slug
        key = f"{self.key}.{slug}"
        cap = f"doctrine:{slug}"
        flyable = num(d.get("flyable"))
        hulls = num(d.get("hulls_in_stock"))
        inputs = [
            LimitInput("pilots_qualified", flyable, "pilots",
                       {"source": "doctrine", "path": f"doctrines[{slug}].flyable"}),
            LimitInput("hulls_in_stock", hulls, "hulls",
                       {"source": "doctrine", "path": f"doctrines[{slug}].hulls_in_stock"}),
        ]
        label_params = {"doctrine": name}
        # Honest-data rule: both candidate limits are required to field a fleet.
        if flyable is None or hulls is None:
            missing = "pilots_qualified" if flyable is None else "hulls_in_stock"
            detail_params = {"doctrine": name, "missing": missing}
            return Constraint(
                key=key, category=self.category,
                label=messages.english(_LABEL, label_params),
                label_key=_LABEL, label_params=label_params,
                status=UNKNOWN, severity="info", affected_capabilities=[cap], inputs=inputs,
                detail=messages.english(_DETAIL_UNKNOWN, detail_params),
                detail_key=_DETAIL_UNKNOWN, detail_params=detail_params,
            )

        binding_in = min((i for i in inputs if i.value is not None), key=lambda i: i.value)
        binding = binding_in.value
        importance = PRIMARY if d.get("primary") else SECONDARY
        # Demand seeded from data: explicit target override, else the doctrine's min_pilots.
        demand = num(targets.get(cap))
        if demand is None:
            demand = num(d.get("min_pilots"))
        headroom = (binding - demand) if demand is not None else None
        severity = severity_for(headroom, demand, cfg, importance=importance)
        detail_key = _DETAIL if demand is not None else _DETAIL_NO_DEMAND
        detail_params = {
            "doctrine": name, "binding": binding, "factor": binding_in.name,
            "flyable": flyable, "hulls": hulls,
        }
        if demand is not None:
            detail_params |= {"headroom": f"{headroom:+g}", "demand": f"{demand:g}"}
        return Constraint(
            key=key, category=self.category,
            label=messages.english(_LABEL, label_params),
            label_key=_LABEL, label_params=label_params,
            binding_metric=binding, unit="pilots", limiting_factor=binding_in.name,
            headroom=headroom, score=constraint_score(headroom, demand), severity=severity,
            affected_capabilities=[cap], inputs=inputs,
            detail=messages.english(detail_key, detail_params),
            detail_key=detail_key, detail_params=detail_params,
        )


register_constraint(FleetSizeProvider())
