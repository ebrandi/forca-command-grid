"""``fleet_size.<doctrine>`` — max pilots we can field per doctrine (design doc 05 §4).

Binding-metric (Liebig minimum) over the two facts the doctrine slice provides:
qualified pilots and staged hulls. One :class:`Constraint` per doctrine; demand is
seeded from data (leadership's choice) — an explicit ``demand_targets`` override,
else the doctrine's own ``min_pilots``. A doctrine missing either fact is reported
``unknown`` with the reason, never fabricated.
"""
from __future__ import annotations

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
        # Honest-data rule: both candidate limits are required to field a fleet.
        if flyable is None or hulls is None:
            missing = "pilots_qualified" if flyable is None else "hulls_in_stock"
            return Constraint(
                key=key, category=self.category, label=f"{name} fleet size",
                status=UNKNOWN, severity="info", affected_capabilities=[cap], inputs=inputs,
                detail=f"Cannot compute max {name} fleet: {missing} missing from the doctrine slice.",
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
        return Constraint(
            key=key, category=self.category, label=f"{name} fleet size",
            binding_metric=binding, unit="pilots", limiting_factor=binding_in.name,
            headroom=headroom, score=constraint_score(headroom, demand), severity=severity,
            affected_capabilities=[cap], inputs=inputs,
            detail=(
                f"Max {name} fleet = {binding} pilots, limited by {binding_in.name} "
                f"({flyable} qualified, {hulls} hulls staged)"
                + (f"; headroom {headroom:+g} vs {demand:g}-pilot demand." if demand is not None
                   else " (no demand target set).")
            ),
        )


register_constraint(FleetSizeProvider())
