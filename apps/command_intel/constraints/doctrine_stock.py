"""``doctrine_stock.<doctrine>`` — fleets-worth of hulls staged (design doc 05 §4).

Binding metric is integer fleets the staged hulls can crew: ``hulls_in_stock //
min_pilots``. Demand is one ready fleet (config-overridable). One :class:`Constraint`
per doctrine; the limiting factor is always ``hulls_in_stock`` — the lever an action
relieves by staging more hulls.
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

# Seam B (``..messages``): every persisted sentence is a scaffold key + JSON-safe params, and the
# English prose column is derived from the msgid itself, so the two can never drift. Numbers the
# English formats (``{x:+g}``/``{x:g}``) are pre-formatted here — the msgid carries no format spec.
_LABEL = "constraint.doctrine_stock.label"
_DETAIL = "constraint.doctrine_stock.detail"
_DETAIL_UNKNOWN = "constraint.doctrine_stock.detail.unknown"


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
        label_params = {"doctrine": name}
        if hulls is None or min_pilots is None:
            missing = "hulls_in_stock" if hulls is None else "min_pilots"
            detail_params = {"doctrine": name, "missing": missing}
            return Constraint(
                key=key, category=self.category,
                label=messages.english(_LABEL, label_params),
                label_key=_LABEL, label_params=label_params,
                status=UNKNOWN, severity="info", affected_capabilities=[cap], inputs=inputs,
                detail=messages.english(_DETAIL_UNKNOWN, detail_params),
                detail_key=_DETAIL_UNKNOWN, detail_params=detail_params,
            )

        per_fleet = max(int(min_pilots), 1)
        binding = int(hulls) // per_fleet
        importance = PRIMARY if d.get("primary") else SECONDARY
        headroom = binding - demand
        severity = severity_for(headroom, demand, cfg, importance=importance)
        detail_params = {
            "hulls": int(hulls), "doctrine": name, "fleets": binding, "per_fleet": per_fleet,
            "headroom": f"{headroom:+g}", "demand": f"{demand:g}",
        }
        return Constraint(
            key=key, category=self.category,
            label=messages.english(_LABEL, label_params),
            label_key=_LABEL, label_params=label_params,
            binding_metric=binding, unit="fleets", limiting_factor="hulls_in_stock",
            headroom=headroom, score=constraint_score(headroom, demand), severity=severity,
            affected_capabilities=[cap], inputs=inputs,
            detail=messages.english(_DETAIL, detail_params),
            detail_key=_DETAIL, detail_params=detail_params,
        )


register_constraint(DoctrineStockProvider())
