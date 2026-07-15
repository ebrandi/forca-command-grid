"""Readiness Digital Twin — deterministic what-if over a snapshot (design doc 17 §1).

Simulation is the **same** constraint engine run over a hypothetical snapshot:

    simulate(scenario) ≜ compute_constraints(perturb(snapshot, scenario)) vs compute_constraints(snapshot)

No second engine, no new model, no ML. Apply a scenario's perturbations to a deep copy of
the latest snapshot's source facts, recompute the binding-metric constraints, and diff. The
perturbations move the same fact-level levers the impact estimator moves (``impact.py`` §2),
so a simulated constraint is computed identically to a live one and the before/after
comparison is trustworthy. The LLM is **not** involved — the numbers are arithmetic over a
dict, instant and explainable.
"""
from __future__ import annotations

import copy

from django.utils.translation import gettext_lazy as _

from . import config, messages
from .engine import pipeline
from .engine.base import SEVERITY_ORDER
from .snapshot import latest_snapshot


# --- perturbations (each mutates a deep copy of the snapshot's source facts) ---
def _doctrines(slices: dict) -> list:
    return (slices.get("doctrine") or {}).get("doctrines") or []


def _apply_pilot_attrition(slices: dict, n: float) -> None:
    """Lose ``n`` qualified pilots from every doctrine (members leave / go inactive)."""
    for d in _doctrines(slices):
        if isinstance(d, dict) and isinstance(d.get("flyable"), int | float):
            d["flyable"] = max(0, int(d["flyable"]) - int(n))


def _apply_lose_staging(slices: dict, pct: float) -> None:
    """Lose ``pct``% of staged hulls (staging contested / assets stranded)."""
    factor = max(0.0, (100.0 - pct) / 100.0)
    for d in _doctrines(slices):
        if isinstance(d, dict) and isinstance(d.get("hulls_in_stock"), int | float):
            d["hulls_in_stock"] = max(0, int(d["hulls_in_stock"] * factor))


def _apply_fuel_shock(slices: dict, factor: float) -> None:
    """Fuel burn multiplies by ``factor`` — every structure's runway shortens."""
    factor = max(1.0, float(factor))
    for s in (slices.get("infrastructure") or {}).get("low_fuel_structures") or []:
        if isinstance(s, dict) and isinstance(s.get("fuel_days_left"), int | float):
            s["fuel_days_left"] = round(s["fuel_days_left"] / factor, 1)


def _apply_income_drop(slices: dict, pct: float) -> None:
    """A ``pct``% hit to the wallet (a lost ratting moon / market crash) shortens runway."""
    fin = slices.get("finance") or {}
    if isinstance(fin.get("balance_isk"), int | float):
        fin["balance_isk"] = max(0.0, fin["balance_isk"] * (100.0 - pct) / 100.0)


# --- scenario registry -------------------------------------------------------
SCENARIOS: dict[str, dict] = {
    "pilot_attrition": {
        "label": _("Pilot attrition"),
        "desc": _("Lose N qualified pilots from every doctrine."),
        "param_label": _("Pilots lost"),
        "default": 5, "min": 1, "max": 200, "apply": _apply_pilot_attrition,
    },
    "lose_staging": {
        "label": _("Lose staging"),
        "desc": _("Lose a percentage of staged hulls (contested staging / stranded assets)."),
        "param_label": _("Hulls lost (%)"),
        "default": 50, "min": 1, "max": 100, "apply": _apply_lose_staging,
    },
    "fuel_shock": {
        "label": _("Fuel shock"),
        "desc": _("Fuel consumption multiplies — structure runways shorten."),
        "param_label": _("Burn multiplier (×)"),
        "default": 2, "min": 1, "max": 10, "apply": _apply_fuel_shock,
    },
    "income_drop": {
        "label": _("Income shock"),
        "desc": _("A one-time hit to the wallet shortens the ISK runway."),
        "param_label": _("Wallet hit (%)"),
        "default": 30, "min": 1, "max": 100, "apply": _apply_income_drop,
    },
}
_DEFAULT_SCENARIO = "pilot_attrition"


def scenario_list(selected: str | None = None, magnitude=None) -> list[dict]:
    """Form-ready scenario metadata (with the current/clamped magnitude marked)."""
    rows = []
    for key, sc in SCENARIOS.items():
        is_sel = key == (selected or _DEFAULT_SCENARIO)
        rows.append({
            "key": key, "label": sc["label"], "desc": sc["desc"],
            "param_label": sc["param_label"], "min": sc["min"], "max": sc["max"],
            "default": sc["default"],
            "value": _clamp(magnitude, sc) if is_sel and magnitude is not None else sc["default"],
            "selected": is_sel,
        })
    return rows


def _clamp(magnitude, scenario: dict) -> float:
    """Parse a (possibly string) magnitude and clamp it to the scenario's bounds."""
    try:
        val = float(magnitude)
    except (TypeError, ValueError):
        return scenario["default"]
    val = max(scenario["min"], min(scenario["max"], val))
    return int(val) if val == int(val) else round(val, 2)


def _diff(before: list, after: list) -> list[dict]:
    """Pair constraints by key; report binding delta + severity change, worsened first."""
    bmap = {c.key: c for c in before}
    amap = {c.key: c for c in after}
    rows: list[dict] = []
    for key in sorted(set(bmap) | set(amap)):
        a = amap.get(key)
        if a is None or a.status != "computed":
            continue
        b = bmap.get(key)
        before_metric = b.binding_metric if b is not None else None
        after_metric = a.binding_metric
        delta = None
        if before_metric is not None and after_metric is not None:
            delta = round(after_metric - before_metric, 2)
        before_sev = b.severity if b is not None else "info"
        worsened = SEVERITY_ORDER.get(a.severity, 0) > SEVERITY_ORDER.get(before_sev, 0)
        rows.append({
            "key": key,
            "label": messages.render(a.label_key, a.label_params, a.label),
            "category": a.category, "unit": a.unit,
            "before": before_metric, "after": after_metric, "delta": delta,
            "before_severity": before_sev, "after_severity": a.severity,
            "worsened": worsened, "headroom_after": a.headroom,
        })
    # Worsened constraints first, then the largest binding drop (most-negative delta).
    rows.sort(key=lambda r: (not r["worsened"], r["delta"] if r["delta"] is not None else 0))
    return rows


def simulate(scenario_key: str | None = None, magnitude=None) -> dict:
    """Run one scenario over the latest snapshot and diff the constraint board.

    Returns ``{available, scenario, magnitude, rows, worsened_count, snapshot}``. Never
    raises: an absent snapshot yields ``available=False`` so the page renders a cold state.
    """
    snapshot = latest_snapshot()
    scenario = SCENARIOS.get(scenario_key or _DEFAULT_SCENARIO) or SCENARIOS[_DEFAULT_SCENARIO]
    key = scenario_key if scenario_key in SCENARIOS else _DEFAULT_SCENARIO
    mag = _clamp(magnitude, scenario)
    if snapshot is None or not (snapshot.slices or {}):
        return {"available": False, "scenario": key, "magnitude": mag, "rows": [],
                "worsened_count": 0, "snapshot": None}

    cfg = config.get("constraints")
    base_slices = snapshot.slices or {}
    before = pipeline.compute_constraints({"sources": base_slices}, cfg)
    perturbed = copy.deepcopy(base_slices)
    scenario["apply"](perturbed, mag)
    after = pipeline.compute_constraints({"sources": perturbed}, cfg)

    rows = _diff(before, after)
    return {
        "available": True, "scenario": key, "magnitude": mag, "rows": rows,
        "worsened_count": sum(1 for r in rows if r["worsened"]), "snapshot": snapshot,
    }


def validate_scenario(scenario_key, magnitude) -> tuple[str, float]:
    """Normalise a (key, magnitude) pair to a valid scenario key + clamped magnitude (4.18).

    An unknown key falls back to the default scenario; the magnitude is clamped to that
    scenario's bounds — so a saved scenario can never carry an out-of-range or bogus input."""
    sc = SCENARIOS.get(scenario_key) if scenario_key in SCENARIOS else None
    key = scenario_key if sc else _DEFAULT_SCENARIO
    return key, _clamp(magnitude, sc or SCENARIOS[_DEFAULT_SCENARIO])


def _unavailable_row(s) -> dict:
    sc = SCENARIOS[_DEFAULT_SCENARIO]
    return {"id": getattr(s, "id", None), "name": s.name, "scenario_label": sc["label"],
            "param_label": sc["param_label"], "magnitude": 0, "available": False,
            "worsened_count": 0, "worst": None, "rows": []}


def compare(saved) -> list[dict]:
    """Run each saved scenario against the LATEST snapshot and summarise for a side-by-side
    contingency comparison (4.18). ``saved`` is an iterable of objects with
    ``name``/``scenario_key``/``magnitude``. Each entry re-runs live (never a stored
    result), so a comparison always reflects current readiness; ranked most-dangerous
    (highest worsened-constraint count) first.

    The snapshot and the unperturbed "before" constraint board are invariant across
    scenarios, so they're computed ONCE (only the per-scenario perturbation + its "after"
    board vary) — review MED fix: avoids N redundant snapshot fetches + "before" recomputes.
    """
    snapshot = latest_snapshot()
    if snapshot is None or not (snapshot.slices or {}):
        return [_unavailable_row(s) for s in saved]

    cfg = config.get("constraints")
    base_slices = snapshot.slices or {}
    before = pipeline.compute_constraints({"sources": base_slices}, cfg)  # computed ONCE

    out: list[dict] = []
    for s in saved:
        key, mag = validate_scenario(s.scenario_key, s.magnitude)
        sc = SCENARIOS[key]
        perturbed = copy.deepcopy(base_slices)
        sc["apply"](perturbed, mag)
        rows = _diff(before, pipeline.compute_constraints({"sources": perturbed}, cfg))
        out.append({
            "id": getattr(s, "id", None), "name": s.name,
            "scenario_label": sc["label"], "param_label": sc["param_label"],
            "magnitude": mag, "available": True,
            "worsened_count": sum(1 for r in rows if r["worsened"]),
            "worst": rows[0] if rows else None,  # rows are worsened-first
            "rows": rows,
        })
    out.sort(key=lambda r: -r["worsened_count"])
    return out
