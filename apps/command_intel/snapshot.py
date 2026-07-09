"""Build, persist and minimise the Intelligence Snapshot (design doc 04 §3-§5).

:func:`build_snapshot` assembles the corp character set once (the readiness
``dimensions.sources.corp_characters`` approach), runs every enabled source through
the isolated :mod:`engine.pipeline`, stamps coverage/versions/config, applies the
leadership-chosen pseudonymisation (doc 04 §5), persists an immutable
``IntelligenceSnapshot`` and caches its id as the latest. :func:`to_contract`
produces the minimised, LLM-facing document (doc 04 §3); :func:`latest_snapshot`
returns the freshest snapshot (cache-first, the readiness "warm" pattern, doc 04 §4).
"""
from __future__ import annotations

import time

from . import config
from .engine import pipeline
from .engine.base import SnapshotContext
from .models import IntelligenceSnapshot
from .pseudonymize import pseudonymize_snapshot

SCHEMA_VERSION = 1
_LATEST_CACHE_KEY = "command_intel:snapshot:latest"
_LATEST_TTL = 3600


def _corp_characters() -> list:
    """The corp character set — reused from the readiness source approach (doc 04 §4)."""
    from apps.sso.models import EveCharacter

    return list(EveCharacter.objects.filter(is_corp_member=True))


def _recognition_optout_ids() -> set[int]:
    """Character ids whose owner opted out of public recognition (doc 04 §5)."""
    from apps.pilots.models import PilotPreference
    from apps.sso.models import EveCharacter

    optout_users = list(
        PilotPreference.objects.filter(public_recognition=False).values_list(
            "user_id", flat=True
        )
    )
    if not optout_users:
        return set()
    return set(
        EveCharacter.objects.filter(user_id__in=optout_users).values_list(
            "character_id", flat=True
        )
    )


def build_snapshot(*, trigger: str = "manual", persist: bool = True, user=None) -> IntelligenceSnapshot:
    """Assemble (and optionally persist) one immutable Intelligence Snapshot.

    Returns the (unsaved when ``persist=False``) model instance with an EPHEMERAL
    ``handle_map`` attribute for the report renderer — the persisted slices are the
    already-pseudonymised ones, and the map is never written to the row (doc 04 §5).
    """
    started = time.monotonic()
    sources_cfg = config.get("sources")
    ctx = SnapshotContext(characters=_corp_characters(), config=sources_cfg)

    raw_slices: dict = {}
    coverage: dict = {}
    versions: dict = {}
    for sl in pipeline.collect_all(ctx, sources_cfg.get("enabled")):
        raw_slices[sl.key] = sl.facts
        coverage[sl.key] = {
            "as_of": sl.as_of,
            "coverage_pct": sl.coverage_pct,
            "status": sl.status,
            "notes": list(sl.notes),
        }
        versions[sl.key] = sl.version

    pseudo_cfg = {
        "pseudonymize_pilots": sources_cfg.get("pseudonymize_pilots", True),
        "include_named_pilots": sources_cfg.get("include_named_pilots", False),
        "optout_ids": _recognition_optout_ids(),
    }
    slices, handle_map = pseudonymize_snapshot(raw_slices, pseudo_cfg)

    snap = IntelligenceSnapshot(
        slices=slices,
        coverage=coverage,
        source_versions=versions,
        config_version=config.config_version(),
        schema_version=SCHEMA_VERSION,
        trigger=trigger,
        build_ms=int((time.monotonic() - started) * 1000),
        built_by=user,
    )
    if persist:
        snap.save()
        from django.core.cache import cache

        cache.set(_LATEST_CACHE_KEY, snap.pk, _LATEST_TTL)

    # Ephemeral re-mapping for the renderer; deliberately NOT a model field.
    snap.handle_map = handle_map
    return snap


def latest_snapshot() -> IntelligenceSnapshot | None:
    """The freshest snapshot — cached id first, newest row as fallback (doc 04 §4)."""
    from django.core.cache import cache

    sid = cache.get(_LATEST_CACHE_KEY)
    if sid is not None:
        snap = IntelligenceSnapshot.objects.filter(pk=sid).first()
        if snap is not None:
            return snap
    return IntelligenceSnapshot.objects.order_by("-created_at").first()


def to_contract(snapshot: IntelligenceSnapshot) -> dict:
    """The minimised, LLM-facing snapshot document (doc 04 §3).

    ``{schema_version, generated_at, sources: {key: {as_of, coverage_pct, status,
    **facts}}, coverage_summary}`` — the persisted (already pseudonymised) slices,
    each fused with its coverage meta, plus a status rollup.
    """
    sources: dict = {}
    rollup = {
        "sources_ok": 0,
        "sources_partial": 0,
        "sources_unknown": 0,
        "sources_unavailable": 0,
    }
    for key, facts in (snapshot.slices or {}).items():
        meta = (snapshot.coverage or {}).get(key, {})
        status = meta.get("status", "unknown")
        sources[key] = {
            "as_of": meta.get("as_of"),
            "coverage_pct": meta.get("coverage_pct"),
            "status": status,
            **(facts or {}),
        }
        bucket = f"sources_{status}"
        rollup[bucket] = rollup.get(bucket, 0) + 1

    generated = snapshot.created_at.isoformat() if snapshot.created_at else None
    return {
        "schema_version": snapshot.schema_version or SCHEMA_VERSION,
        "generated_at": generated,
        "sources": sources,
        "coverage_summary": rollup,
    }
