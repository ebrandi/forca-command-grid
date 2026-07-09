"""Operational-constraint providers (design doc 05).

Each module registers a ``ConstraintProvider`` that computes one or more
binding-metric constraints from a snapshot. Importing this package (from
``AppConfig.ready()``) self-registers them. Adding a constraint = dropping a module
here and listing it below — no pipeline edit.
"""
from __future__ import annotations

# Constraint provider modules are imported here so they self-register on app load.
# Each registers one ConstraintProvider that may emit multiple constraints (e.g. one
# per doctrine). Adding a constraint = dropping a module here and listing it below.
from . import (  # noqa: F401
    doctrine_stock,
    fleet_size,
    fuel_runway,
    isk_runway,
    srp_solvency,
)
