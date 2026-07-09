"""Pingboard configuration — versioned JSON documents over ``admin_audit.AppSetting``.

One row per domain (key ``pingboard.<domain>``), read through this single access
layer with code ``DEFAULTS``, a DEFAULTS-fingerprinted cache, validation,
version-bump-on-write and cache invalidation — the exact ``apps.command_intel.config`` /
``apps.readiness.config`` pattern. Providers and admin never touch ``AppSetting`` directly.

Secrets never live here: per-destination secrets are Fernet-encrypted on
``ChannelProvider``; global provider API tokens are env-only. This layer holds only
non-secret routing/policy knobs, all leadership-tunable without a deploy.

Defaults encode the locked decisions in ``handbooks/administrator-handbook/console-overview.md``.
"""
from __future__ import annotations

import copy

_CACHE_TTL = 600
_VERSION_KEY = "pingboard._version"


class ConfigError(ValueError):
    """Raised on invalid config input, before any write (no partial writes)."""


# --- shipped defaults --------------------------------------------------------
DEFAULTS: dict[str, dict] = {
    # Global switches + dispatch-authority floors + routing defaults.
    "general": {
        "enabled": True,                    # global kill switch
        "manual_alerts_enabled": True,
        "automated_alerts_enabled": True,
        "urgent_alerts_enabled": True,
        "calendar_enabled": True,
        "default_expiry_minutes": 720,      # 12h; 0 = never expire
        "alert_retention_days": 365,        # prune terminal alerts older than this
        "default_channels": ["in_app", "discord"],
        # Minimum role rank to dispatch a given priority tier. Officers do routine
        # traffic; directors own urgent/emergency + corp-wide announcements.
        "dispatch_floor": {
            "low": "officer", "normal": "officer", "high": "officer",
            "urgent": "director", "emergency": "director",
        },
        "announcement_floor": "director",   # corp-wide announcement dispatch floor
        "config_floor": "director",         # who configures providers/rules/templates
        "site_url": "",                     # canonical base URL for links (else CSRF origin)
    },
    # Per-category default audience/channels/priority (the seed matrix). Absent
    # category ⇒ falls back to general.default_channels + corp audience + normal.
    "routing": {
        "categories": {
            "emergency": {"audience": "corp", "channels": ["in_app", "discord", "eve_mail"], "priority": "emergency"},
            "home_defence": {"audience": "corp", "channels": ["in_app", "discord", "eve_mail"], "priority": "urgent"},
            "pvp_fleet": {"audience": "corp", "channels": ["in_app", "discord"], "priority": "high"},
            "roaming_gang": {"audience": "corp", "channels": ["in_app", "discord"], "priority": "high"},
            "gatecamp": {"audience": "corp", "channels": ["in_app", "discord"], "priority": "high"},
            "structure_timer": {"audience": "officer", "channels": ["in_app", "discord"], "priority": "high"},
            "moon_extraction": {"audience": "officer", "channels": ["in_app", "discord"], "priority": "normal"},
            "industry_job": {"audience": "officer", "channels": ["in_app"], "priority": "low"},
            "mining": {"audience": "corp", "channels": ["in_app", "discord"], "priority": "normal"},
            "logistics": {"audience": "user", "channels": ["in_app", "eve_mail"], "priority": "normal"},
            "buyback": {"audience": "user", "channels": ["in_app", "eve_mail"], "priority": "normal"},
            "mentorship": {"audience": "user", "channels": ["in_app"], "priority": "low"},
            "announcement": {"audience": "corp", "channels": ["discord"], "priority": "low"},
            "system": {"audience": "officer", "channels": ["in_app"], "priority": "low"},
        },
    },
    # Anti-spam / rate-limit / retry knobs (numeric defaults from §H).
    "anti_abuse": {
        "max_per_officer_per_hour": 20,
        "max_per_category_per_hour": 30,
        "max_urgent_per_day": 10,
        "cooldown_minutes": 15,             # same audience, repeated alert
        "duplicate_window_minutes": 10,     # same category+audience+body ⇒ suppressed
        "large_audience_threshold": 50,     # extra ack above this resolved count
        "max_retry_attempts": 4,
        "backoff_cap_seconds": 8,
        "two_step_urgent": True,            # emergency requires a second confirm click
        "require_reason_for_urgent": True,
        "approval_required_categories": [],  # categories needing a second-person approve
        "suppress_duplicates": True,
    },
    # Notification-governance policy: who counts as corp leadership, and per-event
    # overrides (enable/disable, audience, severity floor). The event *catalogue* lives
    # in ``apps.pingboard.notifications``; this document only holds the overrides, so an
    # empty document means "every event behaves as catalogued". ``leadership_role`` +
    # ``leadership_user_ids`` define the distribution for leadership-restricted alerts.
    "notifications": {
        "leadership_role": "officer",       # member|officer|director|admin
        "leadership_user_ids": [],          # explicit extra leadership recipients (user ids)
        "events": {},                       # {event_key: {enabled, audience, min_severity}}
    },
    # Calendar policy (used from Phase 3; seeded now so config is complete).
    "calendar": {
        "manual_entries_enabled": True,
        "automated_sync_enabled": True,
        # Auto-generated calendar alerts are DRAFT-until-approved (locked decision E4).
        "auto_alerts_mode": "draft_until_approved",  # draft_until_approved|auto_small|auto_all
        "publishing_services": [
            "operations", "corporation", "erp", "mentorship", "pingboard",
        ],
        "reminder_offsets_minutes": {
            "fleet_op": [1440, 60, 15],
            "emergency_fleet": [60, 15],
            "moon_extraction": [60],
            "structure_timer": [360, 60],
            "industry_job": [30],
            "mentorship": [1440, 60],
        },
        "pilot_visibility": True,           # pilots may see member-visibility events
        "event_retention_days": 90,         # past end_at
    },
}


# --- cache-key fingerprinting ------------------------------------------------
def _fingerprint() -> str:
    import hashlib
    import json

    blob = json.dumps(DEFAULTS, sort_keys=True, default=str).encode()
    return hashlib.sha1(blob).hexdigest()[:8]  # noqa: S324 - cache key, not security


_DEFAULTS_FINGERPRINT = _fingerprint()


def _setting_key(domain: str) -> str:
    return f"pingboard.{domain}"


def _cache_key(domain: str) -> str:
    return f"pingboard:config:{domain}:{_DEFAULTS_FINGERPRINT}"


# --- read --------------------------------------------------------------------
def get(domain: str) -> dict:
    """Merged ``DEFAULTS[domain] ⊕ stored overrides``, deep-copied + cached."""
    if domain not in DEFAULTS:
        raise ConfigError(f"Unknown pingboard config domain: {domain!r}")
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


_ROLE_VALUES = {"public", "member", "officer", "director", "admin"}
_PRIORITIES = {"low", "normal", "high", "urgent", "emergency"}


def _validate_general(value: dict) -> dict:
    v = _ensure_dict(value)
    floor = v.get("dispatch_floor")
    if floor is not None:
        if not isinstance(floor, dict):
            raise ConfigError("general.dispatch_floor must be an object")
        for tier, role in floor.items():
            if tier not in _PRIORITIES:
                raise ConfigError(f"general.dispatch_floor has unknown priority {tier!r}")
            if role not in _ROLE_VALUES:
                raise ConfigError(f"general.dispatch_floor.{tier} invalid role {role!r}")
    for role_key in ("announcement_floor", "config_floor"):
        if role_key in v and v[role_key] not in _ROLE_VALUES:
            raise ConfigError(f"general.{role_key} invalid role {v[role_key]!r}")
    exp = v.get("default_expiry_minutes")
    if exp is not None and (isinstance(exp, bool) or not isinstance(exp, int) or exp < 0):
        raise ConfigError("general.default_expiry_minutes must be a non-negative integer")
    return v


def _validate_anti_abuse(value: dict) -> dict:
    v = _ensure_dict(value)
    ints = (
        "max_per_officer_per_hour", "max_per_category_per_hour", "max_urgent_per_day",
        "cooldown_minutes", "duplicate_window_minutes", "large_audience_threshold",
        "max_retry_attempts", "backoff_cap_seconds",
    )
    for key in ints:
        if key in v:
            n = v[key]
            if isinstance(n, bool) or not isinstance(n, int) or n < 0:
                raise ConfigError(f"anti_abuse.{key} must be a non-negative integer")
    cats = v.get("approval_required_categories")
    if cats is not None and not (isinstance(cats, list) and all(isinstance(c, str) for c in cats)):
        raise ConfigError("anti_abuse.approval_required_categories must be a list of strings")
    return v


_AUDIENCE_VALUES = {"corp", "public", "member", "officer", "director", "admin", "user", "users"}


def _validate_notifications(value: dict) -> dict:
    v = _ensure_dict(value)
    role = v.get("leadership_role")
    if role is not None and role not in _ROLE_VALUES:
        raise ConfigError(f"notifications.leadership_role invalid role {role!r}")
    ids = v.get("leadership_user_ids")
    if ids is not None and not (isinstance(ids, list) and all(
        isinstance(i, int) and not isinstance(i, bool) for i in ids
    )):
        raise ConfigError("notifications.leadership_user_ids must be a list of integers")
    events = v.get("events")
    if events is not None:
        if not isinstance(events, dict):
            raise ConfigError("notifications.events must be an object")
        for key, ov in events.items():
            if not isinstance(ov, dict):
                raise ConfigError(f"notifications.events.{key} must be an object")
            aud = ov.get("audience")
            if aud is not None and aud not in _AUDIENCE_VALUES:
                raise ConfigError(f"notifications.events.{key}.audience invalid {aud!r}")
            sev = ov.get("min_severity")
            if sev is not None and (isinstance(sev, bool) or not isinstance(sev, int) or not 0 <= sev <= 100):
                raise ConfigError(f"notifications.events.{key}.min_severity must be 0–100")
    return v


_VALIDATORS: dict[str, callable] = {
    "general": _validate_general,
    "anti_abuse": _validate_anti_abuse,
    "routing": _ensure_dict,
    "calendar": _ensure_dict,
    "notifications": _validate_notifications,
}


# --- write -------------------------------------------------------------------
def set(domain: str, value: dict, *, user=None) -> dict:  # noqa: A001 - documented public API name
    """Validate + persist a config document, bump the version, bust the cache.

    Raises :class:`ConfigError` before any write. The caller records the audit trail.
    """
    if domain not in DEFAULTS:
        raise ConfigError(f"Unknown pingboard config domain: {domain!r}")
    validated = _VALIDATORS.get(domain, _ensure_dict)(value)

    from apps.admin_audit.models import AppSetting

    AppSetting.objects.update_or_create(
        key=_setting_key(domain), defaults={"value": validated, "updated_by": user}
    )
    _bump_version(user)
    _bust(domain)
    return get(domain)


def reset(domain: str, *, user=None) -> dict:
    if domain not in DEFAULTS:
        raise ConfigError(f"Unknown pingboard config domain: {domain!r}")
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
