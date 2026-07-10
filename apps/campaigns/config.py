"""Campaign Command runtime configuration (design doc 00 §9, doc 05 §4).

Versioned ``admin_audit.AppSetting`` documents, one row per domain (key
``campaigns.<domain>``), read through this single access layer with code ``DEFAULTS``, a
DEFAULTS-fingerprinted cache, validation, version-bump-on-write and cache invalidation — the
exact ``apps.command_intel.config`` / ``apps.readiness.config`` pattern. Services and the admin
console never touch ``AppSetting`` directly.

Everything here is a non-secret, leadership-tunable knob (health thresholds, retention windows,
refresh cadence, recognition defaults). Phase 1 ships only the domains the foundation needs;
later phases add their own domains the same way.
"""
from __future__ import annotations

import copy

_CACHE_TTL = 600
_VERSION_KEY = "campaigns._version"


class ConfigError(ValueError):
    """Raised on invalid config input, before any write (no partial writes)."""


# --- shipped defaults --------------------------------------------------------
# Every domain ships a complete default so the subsystem works before any config exists.
DEFAULTS: dict[str, dict] = {
    # Health-rule thresholds (doc 00 §4). Deterministic evaluation reads these so leadership can
    # retune the health signal without a deploy.
    "health": {
        "deadline_shortfall_pts": 25,   # linear expectation shortfall (pts) → at_risk
        "budget_warn_ratio": 0.95,      # spent/budget at/above this → at_risk
        "budget_critical_ratio": 1.10,  # spent/budget above this → critical
        "inactivity_days": 14,          # no activity for this long → watch
        "stale_multiplier": 2,          # auto-metric stale when age > threshold × this → watch
    },
    # Retention windows for the two append-only, unbounded-growth tables (doc 06 §8). Only
    # archived campaigns are pruned; activity retention never undercuts the 730-day audit floor
    # because sensitive verbs also live in ``admin_audit.AuditLog``.
    "retention": {
        "activity_days": 365,                  # CampaignActivity of archived campaigns
        "samples_days": 180,                   # ObjectiveSample (any campaign)
        "archived_sample_retention_days": 30,  # ObjectiveSample of ARCHIVED campaigns (tighter, doc 06 §8)
    },
    # Metric-refresh cadence + manual-staleness knob (doc 00 §6, doc 08 §2). ``enabled`` is the
    # documented kill-switch (doc 08 §2.1) — off means the refresh beat measures nothing.
    # ``metrics_minutes`` is the default floor an auto objective must age past before the refresh
    # beat re-measures it (the rate limit on the backing services); ``source_minutes`` overrides it
    # per source key. The beat itself runs every 15 min. ``manual_stale_days`` is the confirmation
    # interval after which a manual objective is nudged (the deadline sweep) and surfaces in the
    # officer workspace.
    "refresh": {
        "enabled": True,
        "metrics_minutes": 15,
        "source_minutes": {},
        "manual_stale_days": 14,
    },
    # Notification knobs scoped to Campaign Command (doc 08 §2.2, doc 09 §2.1/§7). Each toggle gates
    # emission *before* pingboard's own governance ``is_enabled`` check — both must pass.
    # ``deadline_reminders`` off means the deadline sweep emits no due-soon/overdue reminders.
    "notifications": {
        "deadline_reminders": True,
    },
    # Recognition defaults applied to a new campaign (doc 00 §5). Stubbed in Phase 1; the
    # recognition flows themselves land in Phase 4.
    "recognition": {
        "default_mode": "none",    # none | counts | points
        "default_public": False,
    },
}


# --- cache key fingerprinting ------------------------------------------------
def _fingerprint() -> str:
    import hashlib
    import json

    blob = json.dumps(DEFAULTS, sort_keys=True, default=str).encode()
    return hashlib.sha1(blob).hexdigest()[:8]  # noqa: S324 - cache key, not security


_DEFAULTS_FINGERPRINT = _fingerprint()


def _setting_key(domain: str) -> str:
    return f"campaigns.{domain}"


def _cache_key(domain: str) -> str:
    return f"campaigns:config:{domain}:{_DEFAULTS_FINGERPRINT}"


# --- read --------------------------------------------------------------------
def get(domain: str) -> dict:
    """Merged ``DEFAULTS[domain] ⊕ stored overrides``, deep-copied + cached."""
    if domain not in DEFAULTS:
        raise ConfigError(f"Unknown campaigns config domain: {domain!r}")
    from django.core.cache import cache

    ck = _cache_key(domain)
    cached = cache.get(ck)
    if cached is None:
        from apps.admin_audit.models import AppSetting

        stored = AppSetting.get(_setting_key(domain), {}) or {}
        cached = _merge(domain, stored)
        cache.set(ck, cached, _CACHE_TTL)
    return copy.deepcopy(cached)


def _merge(domain: str, stored: dict) -> dict:
    """Overlay a stored document on the code defaults (forward-compatible, shallow)."""
    base = copy.deepcopy(DEFAULTS[domain])
    if not isinstance(stored, dict):
        return base
    for key, val in stored.items():
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            merged = dict(base[key])
            merged.update(val)
            base[key] = merged
        else:
            base[key] = val
    return base


def config_version() -> int:
    """Monotonic counter bumped on every write; stamped where a config version is recorded."""
    from apps.admin_audit.models import AppSetting

    return int((AppSetting.get(_VERSION_KEY, {}) or {}).get("version", 0))


def meta(domain: str) -> dict:
    """``{version, by, at}`` for the config-page footer."""
    from apps.admin_audit.models import AppSetting

    row = AppSetting.objects.filter(key=_setting_key(domain)).first()
    by = ""
    if row and row.updated_by_id:
        by = getattr(row.updated_by, "display_name", "") or row.updated_by.get_username()
    return {"version": config_version(), "by": by, "at": row.updated_at if row else None}


# --- validation --------------------------------------------------------------
def _ensure_dict(value) -> dict:
    if not isinstance(value, dict):
        raise ConfigError("config document must be an object")
    return value


def _pos_number(v, name: str) -> None:
    if isinstance(v, bool) or not isinstance(v, int | float) or v <= 0:
        raise ConfigError(f"{name} must be a positive number")


def _pos_int(v, name: str) -> None:
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise ConfigError(f"{name} must be a positive integer")


def _validate_health(value: dict) -> dict:
    v = _ensure_dict(value)
    if "deadline_shortfall_pts" in v:
        n = v["deadline_shortfall_pts"]
        if isinstance(n, bool) or not isinstance(n, int) or not (0 <= n <= 100):
            raise ConfigError("health.deadline_shortfall_pts must be an int in [0, 100]")
    warn = v.get("budget_warn_ratio")
    crit = v.get("budget_critical_ratio")
    if warn is not None:
        _pos_number(warn, "health.budget_warn_ratio")
    if crit is not None:
        _pos_number(crit, "health.budget_critical_ratio")
    # A critical budget breach must sit at or above the warn threshold; otherwise the rules
    # overlap incoherently (a spend could be "critical" but not yet "at_risk").
    if warn is not None and crit is not None and crit < warn:
        raise ConfigError("health.budget_critical_ratio must be >= health.budget_warn_ratio")
    if "inactivity_days" in v:
        _pos_int(v["inactivity_days"], "health.inactivity_days")
    if "stale_multiplier" in v:
        _pos_number(v["stale_multiplier"], "health.stale_multiplier")
    return v


def _validate_retention(value: dict) -> dict:
    v = _ensure_dict(value)
    for key in ("activity_days", "samples_days", "archived_sample_retention_days"):
        if key in v:
            _pos_int(v[key], f"retention.{key}")
    return v


def _validate_refresh(value: dict) -> dict:
    v = _ensure_dict(value)
    if "enabled" in v:
        v["enabled"] = bool(v["enabled"])
    if "metrics_minutes" in v:
        _pos_int(v["metrics_minutes"], "refresh.metrics_minutes")
    if "manual_stale_days" in v:
        _pos_int(v["manual_stale_days"], "refresh.manual_stale_days")
    if "source_minutes" in v:
        overrides = v["source_minutes"]
        if not isinstance(overrides, dict):
            raise ConfigError("refresh.source_minutes must be an object of source-key → minutes")
        for key, minutes in overrides.items():
            _pos_int(minutes, f"refresh.source_minutes.{key}")
    return v


def _validate_recognition(value: dict) -> dict:
    v = _ensure_dict(value)
    mode = v.get("default_mode")
    if mode is not None and mode not in {"none", "counts", "points"}:
        raise ConfigError("recognition.default_mode must be one of none|counts|points")
    if "default_public" in v:
        v["default_public"] = bool(v["default_public"])
    return v


def _validate_notifications(value: dict) -> dict:
    v = _ensure_dict(value)
    if "deadline_reminders" in v:
        v["deadline_reminders"] = bool(v["deadline_reminders"])
    return v


_VALIDATORS: dict[str, callable] = {
    "health": _validate_health,
    "retention": _validate_retention,
    "refresh": _validate_refresh,
    "recognition": _validate_recognition,
    "notifications": _validate_notifications,
}


# --- write -------------------------------------------------------------------
def set(domain: str, value: dict, *, user=None) -> dict:  # noqa: A001 - documented public API name
    """Validate + persist a config document, bump the version, bust the cache.

    Raises :class:`ConfigError` before any write. The caller records the audit trail.
    """
    if domain not in DEFAULTS:
        raise ConfigError(f"Unknown campaigns config domain: {domain!r}")
    validated = _VALIDATORS.get(domain, _ensure_dict)(value)

    from apps.admin_audit.models import AppSetting

    AppSetting.objects.update_or_create(
        key=_setting_key(domain), defaults={"value": validated, "updated_by": user}
    )
    _bump_version(user)
    _bust(domain)
    return get(domain)


def reset(domain: str, *, user=None) -> dict:
    """Restore a domain to its shipped defaults."""
    if domain not in DEFAULTS:
        raise ConfigError(f"Unknown campaigns config domain: {domain!r}")
    from apps.admin_audit.models import AppSetting

    AppSetting.objects.filter(key=_setting_key(domain)).delete()
    _bump_version(user)
    _bust(domain)
    return get(domain)


def _bump_version(user) -> None:
    from apps.admin_audit.models import AppSetting

    current = config_version()
    AppSetting.objects.update_or_create(
        key=_VERSION_KEY, defaults={"value": {"version": current + 1}, "updated_by": user}
    )


def _bust(domain: str) -> None:
    from django.core.cache import cache

    cache.delete(_cache_key(domain))
