"""Auto-metric sources for Campaign Command (design doc 00 §6, doc 08 §2).

Importing this package registers every source — each module calls :func:`base.register` at import,
the ``apps.command_intel.sources`` / ``apps.readiness.dimensions`` discovery idiom. ``apps.py``'s
``ready()`` and ``services`` import it so the registry is populated at startup; adding a source is a
new module here plus its import below, with no edit to the refresh sweep.
"""
from __future__ import annotations

from . import (  # noqa: F401 — imported for their register() side effects
    doctrine,
    finance,
    industry,
    killboard,
    logistics,
    operations,
    readiness,
    srp,
    stockpile,
    structures,
)
from .base import (
    Measurement,
    MetricSource,
    all_sources,
    build_call_params,
    clean_params,
    get_source,
    measure_safely,
    register,
    resolve_choices,
    unregister,
)

__all__ = [
    "Measurement",
    "MetricSource",
    "all_sources",
    "build_call_params",
    "clean_params",
    "get_source",
    "measure_safely",
    "register",
    "resolve_choices",
    "unregister",
]
