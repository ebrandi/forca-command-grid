"""Shared helpers for the constraint providers (design doc 05).

Pure functions over the snapshot dict — no Django imports — so every provider
reads its slices the same way and maps day/month bands to a severity identically.
Kept tiny on purpose: the binding-metric arithmetic lives in each provider.
"""
from __future__ import annotations

from ..engine.base import CRITICAL, HIGH, INFO, WATCH


def domain_sources(snapshot: dict) -> dict:
    """Return the snapshot's per-domain slices, accepting every carrier shape.

    A freshly built snapshot exposes ``{"sources": {...}}``; a persisted one exposes
    ``{"slices": {...}}``; a bare contract dict is itself the slice map. The impact
    engine perturbs a deep copy and re-reads it through this same accessor, so the
    reference it mutates is the one a provider recomputes over.
    """
    if not isinstance(snapshot, dict):
        return {}
    return snapshot.get("sources") or snapshot.get("slices") or snapshot


def slice_facts(snapshot: dict, domain: str) -> dict:
    """The facts dict for one domain (``doctrine``/``finance``/…), or ``{}`` if absent."""
    facts = domain_sources(snapshot).get(domain)
    return facts if isinstance(facts, dict) else {}


def num(value) -> float | int | None:
    """A real number (int/float) as-is, or ``None`` for missing/non-numeric/bool input.

    Preserves ``int`` vs ``float`` so a whole-pilot count stays an int in the binding
    metric. ``bool`` is rejected (it is an ``int`` subclass but never a metric).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    return None


def band_severity(value: float | None, critical: float, high: float, watch: float) -> str:
    """Lower-is-better threshold bands → severity (doc 05 §8: fuel/srp/isk runways).

    ``value`` is days or months remaining; fewer is worse, so it escalates as it falls
    through the configured cut-points. ``None`` (uncomputable) is reported as ``info``.
    """
    if value is None:
        return INFO
    if value <= critical:
        return CRITICAL
    if value <= high:
        return HIGH
    if value <= watch:
        return WATCH
    return INFO
