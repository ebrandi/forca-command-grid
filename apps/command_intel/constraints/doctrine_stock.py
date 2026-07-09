"""``doctrine_stock.<doctrine>`` — fleets-worth of hulls staged (design doc 05 §4).

Binding metric is integer fleets the staged hulls can crew: ``hulls_in_stock //
min_pilots``. Demand is one ready fleet (config-overridable). One :class:`Constraint`
per doctrine; the limiting factor is always ``hulls_in_stock`` — the lever an action
relieves by staging more hulls.
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


class DoctrineStockProvider:
    key = "doctrine_stock"
    label = "Doctrine Stock"
    category = "logistics"
    default_enabled = True

    def compute(self, snapshot: dict, cfg: dict) -> list[Constraint]:
        doctrines = slice_facts(snapshot, "doctrine").get("doctrines") or []
        pcfg = (cfg or {}).get("providers", {}).get(self.key, {})
        demand = num(pcfg.get("demand_fleets"))
        demand = demand if demand is not None else 1
        out: list[Constraint] = []
        for d in doctrines:
            if isinstance(d, dict):
                out.append(self._one(d, demand, cfg))
        return out

    def _one(self, d: dict, demand: float, cfg: dict) -> Constraint:
        slug = d.get("slug") or d.get("name") or "?"
        name = d.get("name") or slug
        key = f"{self.key}.{slug}"
        cap = f"doctrine:{slug}"
        hulls = num(d.get("hulls_in_stock"))
        min_pilots = num(d.get("min_pilots"))
        inputs = [
            LimitInput("hulls_in_stock", hulls, "hulls",
                       {"source": "doctrine", "path": f"doctrines[{slug}].hulls_in_stock"}),
            LimitInput("min_pilots", min_pilots, "pilots",
                       {"source": "doctrine", "path": f"doctrines[{slug}].min_pilots"}),
        ]
        if hulls is None or min_pilots is None:
            missing = "hulls_in_stock" if hulls is None else "min_pilots"
            return Constraint(
                key=key, category=self.category, label=f"{name} stock",
                status=UNKNOWN, severity="info", affected_capabilities=[cap], inputs=inputs,
                detail=f"Cannot compute staged {name} fleets: {missing} missing from the doctrine slice.",
            )

        per_fleet = max(int(min_pilots), 1)
        binding = int(hulls) // per_fleet
        importance = PRIMARY if d.get("primary") else SECONDARY
        headroom = binding - demand
        severity = severity_for(headroom, demand, cfg, importance=importance)
        return Constraint(
            key=key, category=self.category, label=f"{name} stock",
            binding_metric=binding, unit="fleets", limiting_factor="hulls_in_stock",
            headroom=headroom, score=constraint_score(headroom, demand), severity=severity,
            affected_capabilities=[cap], inputs=inputs,
            detail=(
                f"{int(hulls)} {name} hulls staged crew {binding} full fleet(s) "
                f"({per_fleet} pilots each); headroom {headroom:+g} vs {demand:g}-fleet demand."
            ),
        )


register_constraint(DoctrineStockProvider())
