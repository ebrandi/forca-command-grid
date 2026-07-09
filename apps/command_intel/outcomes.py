"""Outcome measurement + the calibration loop (design doc 10 §3, §4).

When an accepted COA's tasks complete, ``measure_outcome`` builds a fresh outcome
snapshot, recomputes the COA's constraint over the baseline and outcome snapshots,
and records the predicted-vs-measured binding delta as an ``ActionOutcome``. Over
many outcomes, ``calibration_for`` turns that history into a per-action-family bias
correction + a confidence factor — so estimates improve with use, without any ML
(transparent statistics, the readiness philosophy).
"""
from __future__ import annotations

from django.utils import timezone

from . import config
from .engine import pipeline
from .models import ActionOutcome, CourseOfAction


def family_of(constraint_key: str) -> str:
    """The action family a constraint belongs to, e.g. ``fleet_size.ferox`` → ``fleet_size``."""
    return (constraint_key or "").split(".", 1)[0]


def _binding_for(snapshot_obj, constraint_key: str) -> float | None:
    """Recompute constraints over a stored snapshot and read one binding metric."""
    if snapshot_obj is None:
        return None
    cons = pipeline.compute_constraints({"sources": snapshot_obj.slices}, config.get("constraints"))
    for c in cons:
        if c.key == constraint_key and c.binding_metric is not None:
            return float(c.binding_metric)
    return None


def measure_outcome(coa: CourseOfAction):
    """Measure a completed COA's actual effect vs its prediction (doc 10 §3).

    Builds a fresh outcome snapshot, diffs the COA's constraint binding against the
    baseline captured at acceptance, and writes an ``ActionOutcome``. Returns it, or
    ``None`` when the COA can't be measured (no baseline / no linked constraint /
    constraint no longer computable). Never raises into the caller.
    """
    if not coa.baseline_snapshot_id or not coa.constraint_id:
        return None
    from . import snapshot as snapshot_mod

    ck = coa.constraint.key
    before = _binding_for(coa.baseline_snapshot, ck)
    outcome_snap = snapshot_mod.build_snapshot(trigger="outcome", persist=True)
    after = _binding_for(outcome_snap, ck)
    if before is None or after is None:
        return None

    measured = round(after - before, 2)
    predicted = float(coa.readiness_delta or 0)
    return ActionOutcome.objects.create(
        coa=coa,
        metric_key=ck,
        predicted_delta=predicted,
        measured_delta=measured,
        error=round(measured - predicted, 2),
        baseline_snapshot=coa.baseline_snapshot,
        outcome_snapshot=outcome_snap,
        measured_at=timezone.now(),
    )


def calibration_for(family: str) -> dict:
    """Learned bias + confidence factor for an action family (doc 10 §4).

    ``bias`` = mean(measured − predicted); applied as a correction only once enough
    samples exist. ``factor`` ∈ [0.4, 1.0] from the error spread (tight history →
    high-confidence estimates). With no/thin history it is a no-op (bias 0, factor 1).
    """
    min_samples = int(config.get("impact").get("calibration_min_samples", 5))
    errors = [
        float(e) for e in ActionOutcome.objects.filter(
            metric_key__startswith=family
        ).values_list("error", flat=True)
    ]
    n = len(errors)
    if n == 0:
        return {"family": family, "n": 0, "bias": 0.0, "spread": 0.0, "factor": 1.0}
    bias = sum(errors) / n
    spread = (sum((e - bias) ** 2 for e in errors) / n) ** 0.5
    if n < min_samples:
        # Not enough history to trust a correction yet — neutral, but report the spread.
        return {"family": family, "n": n, "bias": 0.0, "spread": round(spread, 2), "factor": 1.0}
    factor = max(0.4, min(1.0, 1.0 / (1.0 + spread)))
    return {"family": family, "n": n, "bias": round(bias, 2), "spread": round(spread, 2),
            "factor": round(factor, 2)}


def calibration_summary() -> list[dict]:
    """Per-family calibration view for the Director dashboard (doc 10 §4)."""
    families = sorted({
        family_of(k) for k in ActionOutcome.objects.values_list("metric_key", flat=True).distinct()
    })
    return [calibration_for(f) for f in families]
