"""Readiness scoring engine: generic provider/registry/pipeline primitives.

This package is deliberately **domain-free** — it knows nothing about doctrines,
stockpiles or EVE. Concrete dimensions live in ``apps.readiness.dimensions`` and
depend on this package (one direction only), so a new dimension is added by
writing a provider, never by editing the engine (design principle: extensibility).
"""
