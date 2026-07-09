"""Command Intelligence engine — pure-Python source/constraint abstractions.

The deterministic layer (design docs 04, 05): source providers assemble the
Intelligence Snapshot from existing services; constraint providers compute
Operational Constraints from a snapshot. No Django imports live here so the engine
stays generic and unit-testable in isolation — the same discipline as
``apps.readiness.engine``.
"""
