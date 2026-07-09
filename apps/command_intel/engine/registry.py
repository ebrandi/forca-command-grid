"""Source + constraint provider registries (design docs 04 §1, 05 §2).

Providers self-register on import (each in ``apps.command_intel.sources.<key>`` or
``apps.command_intel.constraints.<key>``); the app's ``ready()`` imports both
packages so discovery happens at startup. The pipeline iterates the registries and
never references a source or constraint by name — adding one is a pure
registration, no pipeline edit. Mirrors ``apps.readiness.engine.registry``.
"""
from __future__ import annotations

from .base import ConstraintProvider, SourceProvider

_SOURCES: dict[str, SourceProvider] = {}
_CONSTRAINTS: dict[str, ConstraintProvider] = {}


# --- sources -----------------------------------------------------------------
def register_source(provider: SourceProvider) -> SourceProvider:
    """Register (or replace, by ``key``) a source provider. Idempotent."""
    _SOURCES[provider.key] = provider
    return provider


def sources() -> list[SourceProvider]:
    return list(_SOURCES.values())


def get_source(key: str) -> SourceProvider | None:
    return _SOURCES.get(key)


# --- constraints -------------------------------------------------------------
def register_constraint(provider: ConstraintProvider) -> ConstraintProvider:
    """Register (or replace, by ``key``) a constraint provider. Idempotent."""
    _CONSTRAINTS[provider.key] = provider
    return provider


def constraints() -> list[ConstraintProvider]:
    return list(_CONSTRAINTS.values())


def get_constraint(key: str) -> ConstraintProvider | None:
    return _CONSTRAINTS.get(key)


# --- test helpers ------------------------------------------------------------
def unregister_source(key: str) -> None:
    _SOURCES.pop(key, None)


def unregister_constraint(key: str) -> None:
    _CONSTRAINTS.pop(key, None)
