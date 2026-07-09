"""Deterministic pipeline: collect the snapshot, compute the constraints.

Per-provider isolation (the readiness pipeline rule): a source/constraint provider
that raises degrades to an ``unavailable`` result and is logged — one bad provider
never fails the whole run. Pure orchestration over the registries; the Django-aware
``snapshot.py`` / ``services.py`` call these and persist the results.
"""
from __future__ import annotations

import logging

from .base import (
    UNAVAILABLE,
    SnapshotContext,
    SourceProvider,
    SourceSlice,
)
from .registry import constraints as _constraint_providers
from .registry import sources as _source_providers

logger = logging.getLogger("forca.command_intel")


def _enabled(provider, cfg: dict, default: bool) -> bool:
    """A provider is enabled unless its config entry says otherwise.

    ``cfg`` is the per-domain ``{key: {"enabled": bool}}`` map. A key absent from the
    config falls back to the provider's ``default_enabled`` (net-new providers ship
    ``default_enabled=False`` and stay off until leadership enables them).
    """
    entry = (cfg or {}).get(provider.key)
    if entry is None:
        return getattr(provider, "default_enabled", default)
    return bool(entry.get("enabled", getattr(provider, "default_enabled", default)))


def collect_slice(provider: SourceProvider, ctx: SnapshotContext) -> SourceSlice:
    """Run one source provider with isolation; degrade to ``unavailable`` on raise."""
    try:
        return provider.collect(ctx)
    except Exception:  # noqa: BLE001 - isolation: one bad source never fails the build
        logger.exception("command_intel source %r raised; degraded to unavailable", provider.key)
        return SourceSlice(
            key=provider.key, version=0, facts={}, status=UNAVAILABLE,
            notes=("provider raised — see logs",),
        )


def collect_all(ctx: SnapshotContext, sources_cfg: dict | None = None) -> list[SourceSlice]:
    """Collect every enabled source into a list of slices (isolated)."""
    sources_cfg = sources_cfg or {}
    out: list[SourceSlice] = []
    for provider in _source_providers():
        if not _enabled(provider, sources_cfg, default=getattr(provider, "default_enabled", True)):
            continue
        out.append(collect_slice(provider, ctx))
    return out


def compute_constraints(snapshot: dict, cfg: dict | None = None) -> list:
    """Run every enabled constraint provider over a snapshot dict (isolated).

    ``snapshot`` is the persisted snapshot's ``{"sources": {...}, ...}`` shape (or the
    minimised contract — both expose ``sources``). ``cfg`` is ``command_intel.constraints``.
    """
    cfg = cfg or {}
    providers_cfg = cfg.get("providers", {})
    results = []
    for provider in _constraint_providers():
        if not _enabled(provider, providers_cfg, default=getattr(provider, "default_enabled", True)):
            continue
        try:
            results.extend(provider.compute(snapshot, cfg) or [])
        except Exception:  # noqa: BLE001 - isolation
            logger.exception("command_intel constraint %r raised; skipped", provider.key)
    return results
