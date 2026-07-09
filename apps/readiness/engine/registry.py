"""Provider registry (design doc 05 §2.2).

Providers self-register on import (each in ``apps.readiness.dimensions.<key>``);
the app's ``ready()`` imports the package so discovery happens at startup. The
pipeline iterates :func:`providers` and never references a dimension by name —
adding a dimension is a pure registration, no pipeline edit.
"""
from __future__ import annotations

from .base import DimensionProvider

_REGISTRY: dict[str, DimensionProvider] = {}


def register(provider: DimensionProvider) -> DimensionProvider:
    """Register (or replace, by ``key``) a provider. Idempotent across re-imports."""
    _REGISTRY[provider.key] = provider
    return provider


def unregister(key: str) -> None:
    """Remove a provider by key (used by tests that inject a transient provider)."""
    _REGISTRY.pop(key, None)


def providers() -> list[DimensionProvider]:
    """All registered providers, in registration order."""
    return list(_REGISTRY.values())


def get(key: str) -> DimensionProvider | None:
    return _REGISTRY.get(key)


def keys() -> list[str]:
    return list(_REGISTRY.keys())
