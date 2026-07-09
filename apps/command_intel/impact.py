"""Impact estimation by perturb-and-recompute (design doc 09).

Method A only (the default, no readiness coupling): apply the minimal local
perturbation an action would cause to a *copy* of the snapshot, re-run **this one
constraint's** provider over the perturbed snapshot, and diff the binding metric.
Because the constraint engine is a pure function of the snapshot, the projected
relief is grounded — not an LLM guess — and fully deterministic.

``candidate_impacts`` pre-computes the obvious relief for every open constraint so
the report's ``candidate_impacts`` block (doc 04 §3, doc 09 §6) is ready before the
LLM call.
"""
from __future__ import annotations

import copy
import math

from .constraints._common import domain_sources
from .engine.base import COMPUTED, CRITICAL, HIGH, WATCH, Constraint
from .engine.registry import get_constraint

# The action→mutation map (doc 09 §2): which snapshot fact each limiting factor moves.
_LEVERS = {"hulls_in_stock", "pilots_qualified", "fuel_days_left", "srp_budget", "balance_isk"}
_OPEN_SEVERITIES = {WATCH, HIGH, CRITICAL}


def _cfg() -> dict:
    """Deterministic constraint config for the re-run — code defaults, no DB read.

    Binding metrics are ``min``/ratios over the snapshot facts and independent of the
    demand thresholds, so re-running with defaults yields a ``binding_after`` that is
    directly comparable to the original ``binding_metric``.
    """
    from . import config

    return copy.deepcopy(config.DEFAULTS["constraints"])


def _impact_weights() -> dict:
    from . import config

    return copy.deepcopy(config.DEFAULTS["impact"]).get("confidence_weights", {})


def _slug_of(constraint: Constraint) -> str | None:
    parts = constraint.key.split(".", 1)
    return parts[1] if len(parts) == 2 else None


def _find_doctrine(sources: dict, slug: str | None) -> dict | None:
    if slug is None:
        return None
    doctrines = (sources.get("doctrine") or {}).get("doctrines") or []
    for d in doctrines:
        if isinstance(d, dict) and (d.get("slug") == slug or d.get("name") == slug):
            return d
    return None


def _perturb(constraint: Constraint, sources: dict) -> dict | None:
    """Mutate ``sources`` in place for the constraint's limiting factor; describe it.

    Returns ``{amount, action, cost, cost_basis}`` or ``None`` when the factor has no
    relief mapping or its fact is absent. Sizing recovers the demand the constraint was
    scored against (``demand = binding − headroom``) and moves the fact just far enough
    to clear the shortfall.
    """
    factor = constraint.limiting_factor
    binding = constraint.binding_metric
    headroom = constraint.headroom
    if factor not in _LEVERS or binding is None:
        return None
    demand = (binding - headroom) if headroom is not None else binding
    short = max(0.0, demand - binding)  # how far below demand we are

    if factor in ("hulls_in_stock", "pilots_qualified"):
        d = _find_doctrine(sources, _slug_of(constraint))
        if d is None:
            return None
        if factor == "hulls_in_stock":
            hulls = int(d.get("hulls_in_stock") or 0)
            if constraint.key.startswith("doctrine_stock."):
                per_fleet = max(int(d.get("min_pilots") or 1), 1)
                target = math.ceil(demand) * per_fleet  # hulls to crew the demanded fleets
                amount = max(1, target - hulls)
            else:  # fleet_size: hulls map 1:1 to fielded pilots
                amount = max(1, math.ceil(demand) - hulls)
            d["hulls_in_stock"] = hulls + amount
            return {"amount": amount, "action": f"Stage {amount} more hull(s)",
                    "cost": amount, "cost_basis": "hulls"}
        flyable = int(d.get("flyable") or 0)
        amount = max(1, math.ceil(demand) - flyable)
        d["flyable"] = flyable + amount
        return {"amount": amount, "action": f"Train {amount} more pilot(s) into the doctrine",
                "cost": amount, "cost_basis": "pilots"}

    if factor == "fuel_days_left":
        structures = (sources.get("infrastructure") or {}).get("low_fuel_structures") or []
        usable = [s for s in structures if isinstance(s, dict) and isinstance(s.get("fuel_days_left"), int | float)]
        if not usable:
            return None
        worst = min(usable, key=lambda s: s["fuel_days_left"])
        amount = max(1, math.ceil(short))
        worst["fuel_days_left"] = worst["fuel_days_left"] + amount
        return {"amount": amount, "action": f"Refuel {worst.get('name', 'structure')} by {amount} day(s)",
                "cost": amount, "cost_basis": "days"}

    if factor == "srp_budget":
        srp = sources.get("srp") or {}
        budget = srp.get("budget_isk")
        spent = srp.get("spent_period_isk")
        if not isinstance(budget, int | float) or not isinstance(spent, int | float):
            return None
        amount = round(max(spent, 1) * short, 0)  # ISK to buy `short` months of cover
        if amount <= 0:
            return None
        srp["budget_isk"] = budget + amount
        return {"amount": amount, "action": f"Top up the SRP budget by {amount:,.0f} ISK",
                "cost": amount, "cost_basis": "isk"}

    # balance_isk (isk_runway)
    fin = sources.get("finance") or {}
    balance, net = fin.get("balance_isk"), fin.get("net_30d_isk")
    if not isinstance(balance, int | float) or not isinstance(net, int | float) or net >= 0:
        return None
    burn = abs(net) / 30
    amount = round(burn * short, 0)  # ISK to buy `short` days of runway
    if amount <= 0:
        return None
    fin["balance_isk"] = balance + amount
    return {"amount": amount, "action": f"Inject {amount:,.0f} ISK of income", "cost": amount, "cost_basis": "isk"}


def _confidence(constraint: Constraint) -> float:
    """Weighted blend of method fidelity, source coverage and calibration (doc 09 §5).

    Method A is an exact re-run; coverage falls when the constraint itself is only
    ``unknown``; calibration sits at a neutral-moderate prior in P1 (no outcome history
    to learn from yet — honestly moderate first estimates, doc 09 §5).
    """
    w = _impact_weights()
    method = 0.9
    coverage = 1.0 if constraint.status == COMPUTED else 0.4
    calibration = 0.7
    score = (w.get("method", 0.4) * method
             + w.get("coverage", 0.3) * coverage
             + w.get("calibration", 0.3) * calibration)
    return round(max(0.0, min(1.0, score)), 2)


def _roi(delta: float, cost, cost_basis: str) -> float | None:
    if not cost or cost <= 0:
        return None
    if cost_basis == "isk":
        return round(delta / (cost / 1e9), 4)  # binding delta per billion ISK
    return round(delta / cost, 4)              # binding delta per unit of effort


def estimate_impact(constraint: Constraint, snapshot: dict, action_hint: str | None = None) -> dict:
    """Project the relief of one constraint by perturb-and-recompute (Method A).

    Returns ``{constraint_key, expected_delta, binding_before, binding_after,
    confidence, roi, action, cost, cost_basis}``. Never raises: an unsupported factor
    or a degraded constraint yields ``status="unknown"`` with a reason.
    """
    base = {
        "constraint_key": constraint.key, "limiting_factor": constraint.limiting_factor,
        "expected_delta": {}, "binding_before": constraint.binding_metric,
        "binding_after": constraint.binding_metric, "confidence": _confidence(constraint),
        "roi": None, "action": action_hint or "",
    }
    provider = get_constraint(constraint.key.split(".", 1)[0])
    if provider is None or constraint.status != COMPUTED:
        return {**base, "status": "unknown", "reason": "no provider or constraint not computed"}

    perturbed = copy.deepcopy(snapshot)
    plan = _perturb(constraint, domain_sources(perturbed))
    if plan is None:
        return {**base, "status": "unknown", "reason": f"no relief mapping for {constraint.limiting_factor!r}"}

    recomputed = provider.compute(perturbed, _cfg()) or []
    after = next((c for c in recomputed if c.key == constraint.key), None)
    if after is None or after.binding_metric is None:
        return {**base, "status": "unknown", "reason": "constraint did not recompute"}

    before = constraint.binding_metric
    delta = round(after.binding_metric - before, 2)
    cost, cost_basis = plan["cost"], plan["cost_basis"]
    return {
        "constraint_key": constraint.key, "limiting_factor": constraint.limiting_factor,
        "status": "computed",
        "expected_delta": {"constraints": {constraint.key: delta}, "unit": constraint.unit},
        "binding_before": before, "binding_after": after.binding_metric,
        "confidence": _confidence(constraint), "roi": _roi(delta, cost, cost_basis),
        "action": action_hint or plan["action"], "cost": cost, "cost_basis": cost_basis,
    }


def candidate_impacts(constraints: list[Constraint], snapshot: dict) -> list[dict]:
    """One precomputed relief impact per open constraint (doc 09 §6).

    Open = a computed constraint at ``watch``/``high``/``critical``. Each is perturbed
    by its obvious relief action and recomputed; constraints with no relief mapping
    (e.g. an ``info`` runway, an unknown) are skipped.
    """
    out: list[dict] = []
    for c in constraints:
        if c.status == COMPUTED and c.severity in _OPEN_SEVERITIES:
            impact = estimate_impact(c, snapshot)
            if impact.get("status") == "computed":
                out.append(impact)
    return out
