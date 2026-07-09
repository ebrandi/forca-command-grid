"""Corporation readiness index — public API over the scoring engine.

Since Phase 0 this is a thin wrapper over the provider/registry **pipeline**
(``apps.readiness.engine`` + ``apps.readiness.dimensions``): it builds the shared
per-run context, runs the providers, and keeps the v1 ``compute_readiness``
signature and ``{index, dimensions, coverage, gaps}`` return shape so existing
callers (the ``/readiness/`` dashboard, the pilots briefing, the ``readiness.warm``
beat task) are untouched. Caching and history persistence live here; the engine
itself stays free of Django cache/model imports.
"""
from __future__ import annotations

_CACHE_KEY = "readiness:index:v1"
_CACHE_TTL = 900  # 15 min — skills sync at most twice a day, so this is plenty fresh


def compute_readiness(persist: bool = False, *, use_cache: bool = True, refresh: bool = False) -> dict:
    """Compute the composite index, dimensions, coverage and ranked gaps.

    The computation scans every corp member against every active doctrine, so the
    read-only result is cached (and warmed by a beat task). ``persist=True`` always
    recomputes and writes a history snapshot; ``refresh=True`` recomputes and
    re-caches without reading the cache first (used by the warmer).
    """
    from django.core.cache import cache

    if use_cache and not persist and not refresh:
        cached = cache.get(_CACHE_KEY)
        if cached is not None:
            return cached

    # Findings are upserted only on the authoritative recomputes (the warm beat's
    # ``refresh`` and the recompute button's ``persist``), never on an opportunistic
    # cache-fill — so the durable risk register tracks the scheduled state, not
    # read traffic.
    result = _run_pipeline(persist=persist, upsert=persist or refresh)
    if use_cache:
        cache.set(_CACHE_KEY, result, _CACHE_TTL)
    return result


def _run_pipeline(persist: bool = False, *, upsert: bool = False) -> dict:
    from .dimensions import sources
    from .engine import pipeline

    config = _config_snapshot()
    ctx = sources.build_context(config)
    results, payload = pipeline.execute(ctx, config)
    if upsert:
        from .findings import upsert_findings

        # Stamp each finding with the score of the KPI it represents (when it maps to
        # one), so score-precise alert rules can match — done centrally here rather than
        # in every provider.
        for r in results:
            kpi_scores = {k.key: k.score for k in r.kpis}
            for f in r.findings:
                if f.kpi_key in kpi_scores:
                    f.score = kpi_scores[f.kpi_key]
        # Only dimensions whose provider actually ran may auto-resolve their findings
        # (a raised provider emits nothing — its register must be left intact).
        ran = {r.key for r in results if r.computed}
        upsert_findings([f for r in results for f in r.findings], ran_dimensions=ran)
        # Forecast findings come from the snapshot-history trend, not provider emission.
        try:
            from .forecast import forecast_findings

            forecast_findings()
        except Exception:  # noqa: BLE001 - forecasting must never block the compute
            import logging

            logging.getLogger(__name__).exception("readiness forecast pass failed")
    if persist:
        _persist_snapshot(payload, config)
    return payload


def _config_snapshot() -> dict:
    """Resolved config for a run, read from the leadership-tunable config layer.

    Builds the engine-facing config dict from the ``readiness.dimensions`` /
    ``readiness.scoring`` documents: ``dimensions`` (enabled + thresholds) drives
    which providers run, the flat ``weights`` map drives ``combine``, and ``version``
    is stamped into the persisted snapshot. With the default (all-equal-weight)
    config this reproduces the Phase-0 equal-weight index exactly.
    """
    from . import config

    dimensions = config.get("dimensions")
    return {
        "dimensions": dimensions,
        # Only enabled dimensions contribute to the index, so the recorded weights
        # (and the combine() lookup) cover exactly those.
        "weights": {
            k: float(v.get("weight", 1.0))
            for k, v in dimensions.items()
            if v.get("enabled", True)
        },
        "scoring": config.get("scoring"),
        # Per-KPI overrides (enable/weight/thresholds) the providers fold into each
        # dimension's score; empty by default ⇒ engine unchanged.
        "kpis": config.get("kpis"),
        "version": config.config_version(),
    }


def _persist_snapshot(result: dict, config: dict) -> None:
    from .models import ReadinessSnapshot

    ReadinessSnapshot.objects.create(
        index=result["index"],
        dimensions=result["dimensions"],
        coverage=result["coverage"],
        kpis=result.get("kpis", {}),
        weights=config.get("weights", {}),
        config_version=config.get("version", 0),
    )


def compute_dimension(key: str):
    """Run a single dimension's provider on demand → its ``DimensionResult`` (or None).

    Used by the drill-down so leadership can preview a dimension's KPIs and findings
    even while it is disabled (excluded from the index). Isolation-safe: a provider
    that raises yields ``None`` rather than erroring the page.
    """
    from .dimensions import sources
    from .engine import registry

    provider = registry.get(key)
    if provider is None:
        return None
    config = _config_snapshot()
    ctx = sources.build_context(config)
    try:
        return provider.compute(ctx)
    except Exception:  # noqa: BLE001 - a drill-down must never 500 on a provider bug
        import logging

        logging.getLogger(__name__).exception("readiness drill-down provider %r failed", key)
        return None


# --- RDY-2 (2.6): guided "activate readiness" wizard -------------------------
_WIZARD_CACHE_KEY = "readiness:activation_preview:v1"


def invalidate_activation_preview() -> None:
    from django.core.cache import cache

    cache.delete(_WIZARD_CACHE_KEY)


def activation_preview() -> list[dict]:
    """For each DISABLED readiness dimension, its would-be live score + recommended
    starter weight, so a director can evaluate the value before enabling it (RDY-2).

    The context is built ONCE and every disabled provider is scored against it (rather
    than N fresh contexts), and the result is briefly cached — this reads member data,
    so it must not recompute on every wizard render.
    """
    from django.core.cache import cache

    cached = cache.get(_WIZARD_CACHE_KEY)
    if cached is not None:
        return cached

    from .config import DEFAULTS
    from .config import get as config_get
    from .dimensions import sources
    from .engine import registry

    cfg = config_get("dimensions")
    defaults = DEFAULTS["dimensions"]
    ctx = sources.build_context(_config_snapshot())
    rows: list[dict] = []
    for provider in registry.providers():
        entry = cfg.get(provider.key, {}) or {}
        if entry.get("enabled", True):
            continue  # only the disabled dimensions are candidates for activation
        try:
            result = provider.compute(ctx)
        except Exception:  # noqa: BLE001 - a preview must never 500 on a provider bug
            result = None
        rows.append({
            "key": provider.key,
            "label": getattr(provider, "label", provider.key.title()),
            "score": None if result is None else result.score,
            "status": "unavailable" if result is None else result.status,
            "recommended_weight": float((defaults.get(provider.key, {}) or {}).get("weight", 1.0)),
            "current_weight": float(entry.get("weight", 1.0)),
        })
    cache.set(_WIZARD_CACHE_KEY, rows, 300)
    return rows


def enable_dimension(key: str) -> bool:
    """Enable a currently-disabled dimension at its recommended weight (one-click from the
    wizard). Returns True if it was enabled, False if the key is unknown or already on."""
    from .config import DEFAULTS
    from .config import get as config_get
    from .config import set as config_set
    from .engine import registry

    if registry.get(key) is None:
        return False
    dims = config_get("dimensions")
    entry = dict(dims.get(key, {}) or {})
    if entry.get("enabled", True):
        return False  # already enabled (or unknown → default-on); nothing to do
    entry["enabled"] = True
    entry.setdefault(
        "weight", float((DEFAULTS["dimensions"].get(key, {}) or {}).get("weight", 1.0))
    )
    new_dims = {**dims, key: entry}
    config_set("dimensions", new_dims)
    invalidate_activation_preview()
    return True


def index_trend(limit: int = 30) -> list[int]:
    from .models import ReadinessSnapshot

    rows = list(ReadinessSnapshot.objects.order_by("-created_at")[:limit])
    return [r.index for r in reversed(rows)]
