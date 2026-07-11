"""Capsuleer Path runtime configuration (brief §9).

Versioned ``admin_audit.AppSetting`` documents, one row per domain (key ``capsuleer.<domain>``),
read through this single access layer with code ``DEFAULTS``, a DEFAULTS-fingerprinted cache,
validation, version-bump-on-write and cache invalidation — the exact
``apps.campaigns.config`` / ``apps.command_intel.config`` pattern. Services and the console never
touch ``AppSetting`` directly.

Everything here is a non-secret, leadership-tunable knob (suggestion caps, aggregate suppression
floor, retention windows, disabled built-in templates, per-event notification arming). Phase 1
ships every domain the brief §9 key set names; the domains are consumed as later stages wire the
suggestion engine (Stage 3), leadership aggregates (Stage 4) and the housekeeping beat (Stage 2).
"""
from __future__ import annotations

import copy

_CACHE_TTL = 600
_VERSION_KEY = "capsuleer._version"


class ConfigError(ValueError):
    """Raised on invalid config input, before any write (no partial writes)."""


# --- shipped defaults --------------------------------------------------------
DEFAULTS: dict[str, dict] = {
    # The reconcile beat kill-switch (Stage 2): off means the reconcile sweep credits nothing.
    "reconcile": {
        "enabled": True,
    },
    # Suggestion generation (Stage 3): ``enabled`` is the kill-switch; ``max_open_per_user`` caps
    # the inbox — when full, lower-priority candidates are simply not generated.
    "suggestions": {
        "enabled": True,
        "max_open_per_user": 6,
    },
    # Leadership aggregate small-group suppression floor (Stage 4). A floor of 2 is enforced at
    # save; any group (or its complement) below this renders "fewer than N", never an exact count.
    "leadership": {
        "min_group": 4,
    },
    # Retention windows for the housekeeping beat (Stage 2). Snapshots keep ~13 months of history;
    # closed/expired suggestions and archived-goal activity age out on their own clocks.
    "retention": {
        "snapshots_days": 400,
        "suggestions_days": 60,
        "activity_days": 365,
    },
    # Built-in template keys leadership has hidden from the catalogue/wizard (never hard-deleted).
    "templates": {
        "disabled_keys": [],
    },
    # Per-event notification arming (Stage 3). Disarmed by default (house convention): a personal
    # career event never pings until leadership arms its event on the console.
    "notifications": {
        "enabled": {
            "milestone_reached": False,
            "goal_completed": False,
            "review_due": False,
            "suggestion": False,
        },
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
    return f"capsuleer.{domain}"


def _cache_key(domain: str) -> str:
    return f"capsuleer:config:{domain}:{_DEFAULTS_FINGERPRINT}"


# --- read --------------------------------------------------------------------
def get(domain: str) -> dict:
    """Merged ``DEFAULTS[domain] ⊕ stored overrides``, deep-copied + cached."""
    if domain not in DEFAULTS:
        raise ConfigError(f"Unknown capsuleer config domain: {domain!r}")
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


def _pos_int(v, name: str) -> None:
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise ConfigError(f"{name} must be a positive integer")


def _validate_reconcile(value: dict) -> dict:
    v = _ensure_dict(value)
    if "enabled" in v:
        v["enabled"] = bool(v["enabled"])
    return v


def _validate_suggestions(value: dict) -> dict:
    v = _ensure_dict(value)
    if "enabled" in v:
        v["enabled"] = bool(v["enabled"])
    if "max_open_per_user" in v:
        _pos_int(v["max_open_per_user"], "suggestions.max_open_per_user")
    return v


def _validate_leadership(value: dict) -> dict:
    v = _ensure_dict(value)
    if "min_group" in v:
        _pos_int(v["min_group"], "leadership.min_group")
        # A floor of 2 keeps suppression meaningful — a group of 1 can never be published.
        if v["min_group"] < 2:
            raise ConfigError("leadership.min_group must be at least 2")
    return v


def _validate_retention(value: dict) -> dict:
    v = _ensure_dict(value)
    for key in ("snapshots_days", "suggestions_days", "activity_days"):
        if key in v:
            _pos_int(v[key], f"retention.{key}")
    return v


def _validate_templates(value: dict) -> dict:
    v = _ensure_dict(value)
    if "disabled_keys" in v:
        keys = v["disabled_keys"]
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            raise ConfigError("templates.disabled_keys must be a list of template keys")
    return v


def _validate_notifications(value: dict) -> dict:
    v = _ensure_dict(value)
    if "enabled" in v:
        enabled = v["enabled"]
        if not isinstance(enabled, dict):
            raise ConfigError("notifications.enabled must be an object of event → bool")
        v["enabled"] = {k: bool(val) for k, val in enabled.items()}
    return v


_VALIDATORS: dict[str, callable] = {
    "reconcile": _validate_reconcile,
    "suggestions": _validate_suggestions,
    "leadership": _validate_leadership,
    "retention": _validate_retention,
    "templates": _validate_templates,
    "notifications": _validate_notifications,
}


# --- write -------------------------------------------------------------------
def set(domain: str, value: dict, *, user=None) -> dict:  # noqa: A001 - documented public API name
    """Validate + persist a config document, bump the version, bust the cache.

    Raises :class:`ConfigError` before any write. The caller records the audit trail.
    """
    if domain not in DEFAULTS:
        raise ConfigError(f"Unknown capsuleer config domain: {domain!r}")
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
        raise ConfigError(f"Unknown capsuleer config domain: {domain!r}")
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
