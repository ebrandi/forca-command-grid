"""Comms-access configuration — versioned JSON over ``admin_audit.AppSetting``.

The exact ``apps.pingboard.config`` / ``apps.command_intel.config`` pattern: code
``DEFAULTS``, a DEFAULTS-fingerprinted cache, validation, version-bump-on-write and cache
invalidation. Holds only non-secret arming/policy knobs — bot tokens are env-only
(``DISCORD_BOT_TOKEN``) and per-account OAuth tokens are Fernet-encrypted on
``CommsAccount``. Every knob is leadership-tunable without a deploy.

Domains:

* ``general`` — global kill switch, global dry-run, revoke grace window.
* ``platforms`` — per-platform arming (``armed``, ``guild_id``/``workspace_id``, ``kick_enabled``).
"""
from __future__ import annotations

import copy

from django.utils.translation import gettext as _

_CACHE_TTL = 600

PLATFORMS = ("discord", "slack", "mumble")


class ConfigError(ValueError):
    """Raised on invalid config input, before any write (no partial writes)."""


DEFAULTS: dict[str, dict] = {
    "general": {
        "enabled": False,          # global kill switch — ships inert
        "global_dry_run": True,    # preview everything until leadership flips it off
        "revoke_grace_minutes": 0,  # delay before an authoritative revoke applies
    },
    # Per-platform arming. A platform does nothing until armed AND a provider is registered.
    "platforms": {
        "discord": {"armed": False, "guild_id": "", "kick_enabled": False},
        "slack": {"armed": False, "workspace_id": "", "kick_enabled": False},
        "mumble": {"armed": False, "server_id": "", "kick_enabled": False},
    },
}


def _fingerprint() -> str:
    import hashlib
    import json

    blob = json.dumps(DEFAULTS, sort_keys=True, default=str).encode()
    return hashlib.sha1(blob).hexdigest()[:8]  # noqa: S324 - cache key, not security


_DEFAULTS_FINGERPRINT = _fingerprint()


def _setting_key(domain: str) -> str:
    return f"comms_access.{domain}"


def _cache_key(domain: str) -> str:
    return f"comms_access:config:{domain}:{_DEFAULTS_FINGERPRINT}"


_VERSION_KEY = "comms_access._version"


def get(domain: str) -> dict:
    if domain not in DEFAULTS:
        raise ConfigError(
            _("Unknown comms_access config domain: %(domain)r") % {"domain": domain}
        )
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
    from apps.admin_audit.models import AppSetting

    row = AppSetting.objects.filter(key=_setting_key(domain)).first()
    by = ""
    if row and row.updated_by_id:
        by = getattr(row.updated_by, "display_name", "") or row.updated_by.get_username()
    return {"version": config_version(), "by": by, "at": row.updated_at if row else None}


# --- validation --------------------------------------------------------------
def _ensure_dict(value) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(_("config document must be an object"))
    return value


def _bool(v) -> bool:
    return bool(v) if isinstance(v, bool) else v in ("on", "true", "True", "1", 1)


def _validate_general(value: dict) -> dict:
    v = _ensure_dict(value)
    grace = v.get("revoke_grace_minutes")
    if grace is not None and (isinstance(grace, bool) or not isinstance(grace, int) or grace < 0):
        raise ConfigError(_("general.revoke_grace_minutes must be a non-negative integer"))
    return v


def _validate_platforms(value: dict) -> dict:
    v = _ensure_dict(value)
    for name, cfg in v.items():
        if name not in PLATFORMS:
            raise ConfigError(_("unknown platform %(platform)r") % {"platform": name})
        if not isinstance(cfg, dict):
            raise ConfigError(
                _("platforms.%(platform)s must be an object") % {"platform": name}
            )
    return v


_VALIDATORS = {
    "general": _validate_general,
    "platforms": _validate_platforms,
}


def set(domain: str, value: dict, *, user=None) -> dict:  # noqa: A001 - documented public API
    if domain not in DEFAULTS:
        raise ConfigError(
            _("Unknown comms_access config domain: %(domain)r") % {"domain": domain}
        )
    validated = _VALIDATORS.get(domain, _ensure_dict)(value)

    from apps.admin_audit.models import AppSetting

    AppSetting.objects.update_or_create(
        key=_setting_key(domain), defaults={"value": validated, "updated_by": user}
    )
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


# --- convenience -------------------------------------------------------------
def is_enabled() -> bool:
    """Global feature switch (config), independent of the env kill switch."""
    return bool(get("general").get("enabled"))


def feature_active() -> bool:
    """The effective on/off gate: the ops kill switch permits it *and* leadership enabled it.

    ``COMMS_ACCESS_ENABLED`` defaults on — the Director console (``is_enabled``) is the real
    switch, so leaders arm the feature without touching ``.env``. Ops can still hard-disable
    by setting the env flag to ``False``, which short-circuits before any config read."""
    from django.conf import settings

    if not getattr(settings, "COMMS_ACCESS_ENABLED", True):
        return False
    return is_enabled()


def platform_armed(platform: str) -> bool:
    return bool(get("platforms").get(platform, {}).get("armed"))


def global_dry_run() -> bool:
    return bool(get("general").get("global_dry_run", True))
