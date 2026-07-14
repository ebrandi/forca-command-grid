"""Readiness configuration access layer (design doc 04).

The single reader/writer of readiness config *documents* — weight/threshold/toggle
blobs stored one row per domain in ``admin_audit.AppSetting``. Providers and admin
views call :func:`get`/:func:`set` here and never touch ``AppSetting`` directly.

Defaults ship in code (:data:`DEFAULTS`) so the platform scores correctly before
anything is configured — exactly like ``core/features.py`` defaulting to "everything
on". Crucially the default dimension weights are **equal (1.0)**, so the default
configuration reproduces the Phase-0 equal-weight index byte-for-byte: the config
layer is inert until leadership tunes a weight or disables a dimension.

Every :func:`set` validates types/ranges (raising :class:`ConfigError`, surfaced on
the admin form), persists the document, **bumps ``config_version``** (stamped into
subsequent snapshots so a historical score stays explainable), and busts the
per-process cache. The admin view adds the ``audit_log`` trail.
"""
from __future__ import annotations

import copy

from django.utils.translation import gettext as _

_CACHE_TTL = 600


class ConfigError(ValueError):
    """A config document failed validation; the message is shown on the admin form."""


# One entry per registered dimension. Thresholds drive status colour only (not the
# index), so they are inert for the Phase-0 golden gate. Weights are equal at 1.0 so
# the default index == the Phase-0 equal-weight mean. Future phases that register new
# providers extend this map; a stored override merges over it.
DEFAULTS: dict[str, dict] = {
    "dimensions": {
        "doctrine":  {"enabled": True, "weight": 1.0, "thresholds": {"amber": 60, "red": 40}},
        "skill":     {"enabled": True, "weight": 1.0, "thresholds": {"amber": 60, "red": 40}},
        "stock":     {"enabled": True, "weight": 1.0, "thresholds": {"amber": 55, "red": 35}},
        "logistics": {"enabled": True, "weight": 1.0, "thresholds": {"amber": 60, "red": 40}},
        # Net-new dimensions ship DISABLED (preview via their drill-down; enable on
        # the Dimensions page once verified) so the live index is unchanged until
        # leadership opts in.
        "financial": {"enabled": False, "weight": 1.2, "thresholds": {"amber": 60, "red": 35}},
        "srp":       {"enabled": False, "weight": 0.8, "thresholds": {"amber": 60, "red": 40}},
        "activity":  {"enabled": False, "weight": 1.0, "thresholds": {"amber": 60, "red": 40}},
        "recruitment": {"enabled": False, "weight": 0.7, "thresholds": {"amber": 55, "red": 35}},
        "leadership": {"enabled": False, "weight": 1.0, "thresholds": {"amber": 60, "red": 40}},
        "infrastructure": {"enabled": False, "weight": 0.9, "thresholds": {"amber": 60, "red": 40}},
        "strategic": {"enabled": False, "weight": 0.8, "thresholds": {"amber": 60, "red": 40}},
        "fleet_comp": {"enabled": False, "weight": 1.0, "thresholds": {"amber": 65, "red": 45}},
        # Config-gated dimensions (Gap B4/B5): ship disabled AND stay unavailable until
        # leadership populates their config (fleet-support skill list / staging system).
        "support":   {"enabled": False, "weight": 0.8, "thresholds": {"amber": 60, "red": 40}},
        "staging":   {"enabled": False, "weight": 0.8, "thresholds": {"amber": 60, "red": 40}},
    },
    "scoring": {
        "index_method": "weighted_mean",
        "default_forecast_window_days": 14,
        "forecast_method": "linear",
        "exclude_unknown_from_denominator": True,
        "min_coverage_for_green": 0.5,
        # Auto task-generation anti-spam controls (task-generation doc §8).
        "min_task_weight": 5.0,
        "task_severity_floor": "warn",
        "max_tasks_per_run": 25,
    },
    # Officer-responsibility mapping (config doc §7). A finding's owner resolves
    # kpi_owner → dimension_owner → unassigned. Users default empty (claimable pool)
    # until leadership assigns them; the dimension→owner map ships for the existing
    # dimensions so generated tasks already route to the right desk.
    "responsibilities": {
        "owner_tags": {
            "training_officer":  {"label": "Training Officer", "users": []},
            "industry_director": {"label": "Industry Director", "users": []},
            "logistics_director": {"label": "Logistics Director", "users": []},
            "finance_officer":   {"label": "Finance Officer", "users": []},
            "srp_officer":       {"label": "SRP Officer", "users": []},
            "recruitment_officer": {"label": "Recruitment Officer", "users": []},
        },
        "dimension_owner": {
            "doctrine": "training_officer",
            "skill": "training_officer",
            "stock": "industry_director",
            "logistics": "logistics_director",
        },
        "kpi_owner": {},
    },
    # Alert rules (config doc §8). Empty until Phase 5 / leadership opts in, so the
    # generate_tasks beat ships inert (no rule ⇒ no auto-task).
    "alerts": {"rules": []},
    # Targets the Financial Health dimension scores against (config doc §4). ISK.
    "finance": {
        "min_wallet": 5_000_000_000,
        "monthly_burn_target": 2_000_000_000,
        "srp_budget": 1_000_000_000,
        "emergency_reserve": 3_000_000_000,
        "alliance_payments_monthly": 500_000_000,
        "sov_costs_monthly": 300_000_000,
        "infrastructure_costs_monthly": 400_000_000,
        "wallet_division_scope": "all",
    },
    # Bounds the SRP-health KPIs (config doc §5).
    "srp": {
        "max_pending_claims": 25,
        "max_avg_wait_hours": 72,
        "max_claim_age_days": 14,
    },
    # Per-KPI overrides (config doc §3): {kpi_key: {enabled, weight, thresholds}}.
    # Empty by default ⇒ every KPI is enabled at weight 1.0 with its provider's own
    # status bands, so the default config reproduces the engine exactly (a disabled or
    # re-weighted KPI only changes its dimension's score once leadership tunes it).
    "kpis": {},
    # Notification delivery settings (config doc §8 / doc 13). The EVE-mail sender is
    # the director character chosen to send readiness alert mail in-game; None until
    # leadership picks one (so EVE-mail stays a no-op until configured + scope-granted).
    "notifications": {"eve_mail_sender_character_id": None},
    # Targets the Recruitment dimension scores against (config doc §6).
    "recruitment": {
        "target_active_members": 120,
        "min_monthly_intake": 5,
        "max_dormant_ratio": 0.25,
    },
}

_VERSION_KEY = "readiness.config_version"

# A short fingerprint of the shipped DEFAULTS, baked into the cache key. When the
# code's defaults change (e.g. a new dimension is added) the fingerprint changes,
# so a stale cached merge from the previous build is abandoned automatically — no
# manual cache bust on deploy, and a newly-added dimension can't fall through to
# the "default on" path because the old (smaller) merged doc was cached.
def _fingerprint() -> str:
    import hashlib
    import json

    blob = json.dumps(DEFAULTS, sort_keys=True, default=str).encode()
    return hashlib.sha1(blob).hexdigest()[:8]  # noqa: S324 - cache key, not security


_DEFAULTS_FINGERPRINT = _fingerprint()


def _setting_key(domain: str) -> str:
    return f"readiness.{domain}"


def _cache_key(domain: str) -> str:
    return f"readiness:config:{domain}:{_DEFAULTS_FINGERPRINT}"


# --- read --------------------------------------------------------------------
def get(domain: str) -> dict:
    """Merged ``DEFAULTS[domain] ⊕ stored overrides`` for a domain, cached.

    Returns a deep copy so callers can't mutate the cached document.
    """
    if domain not in DEFAULTS:
        raise ConfigError(
            _("Unknown readiness config domain: %(domain)r") % {"domain": domain}
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
    """Overlay a stored document on the code defaults (forward-compatible).

    For ``dimensions`` the union of keys is taken (so a stored doc predating a newly
    added dimension still surfaces it) and each entry's fields — including the nested
    ``thresholds`` — are overlaid. Flat domains shallow-merge.
    """
    base = copy.deepcopy(DEFAULTS[domain])
    if not isinstance(stored, dict):
        return base
    if domain == "dimensions":
        out: dict[str, dict] = {}
        for key in {*base, *stored}:
            entry = dict(base.get(key, {}))
            override = stored.get(key, {}) or {}
            thresholds = {**entry.get("thresholds", {}), **(override.get("thresholds") or {})}
            entry.update({k: v for k, v in override.items() if k != "thresholds"})
            if thresholds:
                entry["thresholds"] = thresholds
            out[key] = entry
        return out
    base.update(stored)
    return base


def dimension_weights() -> dict[str, float]:
    """``{key: weight}`` for the enabled dimensions (a convenience derived view)."""
    return {
        key: float(entry.get("weight", 1.0))
        for key, entry in get("dimensions").items()
        if entry.get("enabled", True)
    }


def config_version() -> int:
    """Monotonic counter bumped on every write; stamped into snapshots."""
    from apps.admin_audit.models import AppSetting

    return int((AppSetting.get(_VERSION_KEY, {}) or {}).get("version", 0))


def meta(domain: str) -> dict:
    """``{version, by, at}`` for the config-page footer ("config v#N, by …, at …")."""
    from apps.admin_audit.models import AppSetting

    row = AppSetting.objects.filter(key=_setting_key(domain)).first()
    by = ""
    if row and row.updated_by_id:
        by = getattr(row.updated_by, "display_name", "") or row.updated_by.get_username()
    return {"version": config_version(), "by": by, "at": row.updated_at if row else None}


# --- write -------------------------------------------------------------------
def set(domain: str, value: dict, *, user=None) -> dict:  # noqa: A001 - documented public API name
    """Validate + persist a config document, bump the version, and bust the cache.

    Raises :class:`ConfigError` on invalid input *before* any write (no partial
    writes). The caller is responsible for the ``audit_log`` trail.
    """
    if domain not in DEFAULTS:
        raise ConfigError(
            _("Unknown readiness config domain: %(domain)r") % {"domain": domain}
        )
    validated = _VALIDATORS[domain](value)

    from apps.admin_audit.models import AppSetting

    AppSetting.objects.update_or_create(
        key=_setting_key(domain), defaults={"value": validated, "updated_by": user}
    )
    _bump_version(user)
    _bust(domain)
    return get(domain)


def reset(domain: str, *, user=None) -> dict:
    """Restore a domain to its code defaults (a single audited ``set``)."""
    return set(domain, copy.deepcopy(DEFAULTS[domain]), user=user)


def _bump_version(user=None) -> int:
    from apps.admin_audit.models import AppSetting

    current = int((AppSetting.get(_VERSION_KEY, {}) or {}).get("version", 0))
    AppSetting.objects.update_or_create(
        key=_VERSION_KEY, defaults={"value": {"version": current + 1}, "updated_by": user}
    )
    return current + 1


def _bust(domain: str) -> None:
    from django.core.cache import cache

    cache.delete(_cache_key(domain))


# --- validation --------------------------------------------------------------
def _validate_dimensions(value) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(_("Dimensions configuration must be an object."))
    if not value:
        raise ConfigError(_("At least one dimension must be configured."))
    out: dict[str, dict] = {}
    for key, entry in value.items():
        if not isinstance(entry, dict):
            raise ConfigError(_("Dimension %(key)r must be an object.") % {"key": key})
        try:
            weight = float(entry.get("weight", 1.0))
        except (TypeError, ValueError):
            raise ConfigError(_("%(key)s: weight must be a number.") % {"key": key}) from None
        if weight < 0:
            raise ConfigError(_("%(key)s: weight must be ≥ 0.") % {"key": key})
        thresholds = entry.get("thresholds") or {}
        try:
            amber = int(thresholds.get("amber", 60))
            red = int(thresholds.get("red", 40))
        except (TypeError, ValueError):
            raise ConfigError(
                _("%(key)s: amber and red must be whole numbers.") % {"key": key}
            ) from None
        if not (0 <= red < amber <= 100):
            raise ConfigError(
                _(
                    "%(key)s: thresholds must satisfy 0 ≤ red < amber ≤ 100 "
                    "(got red=%(red)s, amber=%(amber)s)."
                )
                % {"key": key, "red": red, "amber": amber}
            )
        out[key] = {
            "enabled": bool(entry.get("enabled", True)),
            "weight": weight,
            "thresholds": {"amber": amber, "red": red},
        }
    return out


def _validate_scoring(value) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(_("Scoring configuration must be an object."))
    out = copy.deepcopy(DEFAULTS["scoring"])
    if "index_method" in value:
        if value["index_method"] not in ("weighted_mean",):
            raise ConfigError(_("Unsupported index_method (only 'weighted_mean' is available)."))
        out["index_method"] = value["index_method"]
    if "min_coverage_for_green" in value:
        try:
            cov = float(value["min_coverage_for_green"])
        except (TypeError, ValueError):
            raise ConfigError(_("min_coverage_for_green must be a number between 0 and 1.")) from None
        if not (0.0 <= cov <= 1.0):
            raise ConfigError(_("min_coverage_for_green must be between 0 and 1."))
        out["min_coverage_for_green"] = cov
    if "default_forecast_window_days" in value:
        try:
            window = int(value["default_forecast_window_days"])
        except (TypeError, ValueError):
            raise ConfigError(_("default_forecast_window_days must be a whole number.")) from None
        if not (1 <= window <= 90):
            raise ConfigError(_("default_forecast_window_days must be between 1 and 90."))
        out["default_forecast_window_days"] = window
    if "exclude_unknown_from_denominator" in value:
        out["exclude_unknown_from_denominator"] = bool(value["exclude_unknown_from_denominator"])
    if "min_task_weight" in value:
        try:
            mtw = float(value["min_task_weight"])
        except (TypeError, ValueError):
            raise ConfigError(_("min_task_weight must be a number.")) from None
        if mtw < 0:
            raise ConfigError(_("min_task_weight must be ≥ 0."))
        out["min_task_weight"] = mtw
    if "task_severity_floor" in value:
        if value["task_severity_floor"] not in ("info", "warn", "high", "critical"):
            raise ConfigError(_("task_severity_floor must be info/warn/high/critical."))
        out["task_severity_floor"] = value["task_severity_floor"]
    if "max_tasks_per_run" in value:
        try:
            mtr = int(value["max_tasks_per_run"])
        except (TypeError, ValueError):
            raise ConfigError(_("max_tasks_per_run must be a whole number.")) from None
        if mtr < 1:
            raise ConfigError(_("max_tasks_per_run must be ≥ 1."))
        out["max_tasks_per_run"] = mtr
    return out


def _validate_responsibilities(value) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(_("Responsibilities configuration must be an object."))
    owner_tags = value.get("owner_tags") or {}
    dimension_owner = value.get("dimension_owner") or {}
    kpi_owner = value.get("kpi_owner") or {}
    if not isinstance(owner_tags, dict) or not isinstance(dimension_owner, dict) or not isinstance(kpi_owner, dict):
        raise ConfigError(_("owner_tags, dimension_owner and kpi_owner must be objects."))
    # Every referenced owner tag must be defined (no dangling references).
    for ref in (*dimension_owner.values(), *kpi_owner.values()):
        if ref and ref not in owner_tags:
            raise ConfigError(
                _("Owner tag %(tag)r is referenced but not defined.") % {"tag": ref}
            )
    return {"owner_tags": owner_tags, "dimension_owner": dimension_owner, "kpi_owner": kpi_owner}


def _validate_alerts(value) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(_("Alerts configuration must be an object."))
    rules = value.get("rules", [])
    if not isinstance(rules, list):
        raise ConfigError(_("Alert rules must be a list."))
    # NB: the module-level ``set`` function shadows the builtin, so dedupe with a list.
    seen_keys: list[str] = []
    for rule in rules:
        if not isinstance(rule, dict) or not rule.get("key"):
            raise ConfigError(_("Each alert rule needs a unique key."))
        if rule["key"] in seen_keys:
            raise ConfigError(
                _("Duplicate alert rule key: %(key)r.") % {"key": rule["key"]}
            )
        seen_keys.append(rule["key"])
    return {"rules": rules}


def _validate_finance(value) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(_("Financial configuration must be an object."))
    out = copy.deepcopy(DEFAULTS["finance"])
    isk_keys = (
        "min_wallet", "monthly_burn_target", "srp_budget", "emergency_reserve",
        "alliance_payments_monthly", "sov_costs_monthly", "infrastructure_costs_monthly",
    )
    for key in isk_keys:
        if key in value:
            try:
                amount = float(value[key])
            except (TypeError, ValueError):
                raise ConfigError(
                    _("%(key)s must be a number (ISK).") % {"key": key}
                ) from None
            if amount < 0:
                raise ConfigError(_("%(key)s must be ≥ 0.") % {"key": key})
            out[key] = amount
    if "wallet_division_scope" in value:
        scope = value["wallet_division_scope"]
        if scope != "all":
            try:
                int(scope)
            except (TypeError, ValueError):
                raise ConfigError(_("wallet_division_scope must be 'all' or a division id.")) from None
        out["wallet_division_scope"] = scope
    return out


def _validate_srp(value) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(_("SRP configuration must be an object."))
    out = copy.deepcopy(DEFAULTS["srp"])
    for key in ("max_pending_claims", "max_avg_wait_hours", "max_claim_age_days"):
        if key in value:
            try:
                amount = int(value[key])
            except (TypeError, ValueError):
                raise ConfigError(
                    _("%(key)s must be a whole number.") % {"key": key}
                ) from None
            if amount < 0:
                raise ConfigError(_("%(key)s must be ≥ 0.") % {"key": key})
            out[key] = amount
    return out


def _validate_recruitment(value) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(_("Recruitment configuration must be an object."))
    out = copy.deepcopy(DEFAULTS["recruitment"])
    for key in ("target_active_members", "min_monthly_intake"):
        if key in value:
            try:
                amount = int(value[key])
            except (TypeError, ValueError):
                raise ConfigError(
                    _("%(key)s must be a whole number.") % {"key": key}
                ) from None
            if amount < 0:
                raise ConfigError(_("%(key)s must be ≥ 0.") % {"key": key})
            out[key] = amount
    if "max_dormant_ratio" in value:
        try:
            ratio = float(value["max_dormant_ratio"])
        except (TypeError, ValueError):
            raise ConfigError(_("max_dormant_ratio must be a number between 0 and 1.")) from None
        if not (0.0 <= ratio <= 1.0):
            raise ConfigError(_("max_dormant_ratio must be between 0 and 1."))
        out["max_dormant_ratio"] = ratio
    return out


def _validate_kpis(value) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(_("KPI configuration must be an object."))
    out: dict[str, dict] = {}
    for key, entry in value.items():
        if not isinstance(entry, dict):
            raise ConfigError(_("KPI %(key)r must be an object.") % {"key": key})
        try:
            weight = float(entry.get("weight", 1.0))
        except (TypeError, ValueError):
            raise ConfigError(_("%(key)s: weight must be a number.") % {"key": key}) from None
        if weight < 0:
            raise ConfigError(_("%(key)s: weight must be ≥ 0.") % {"key": key})
        item: dict = {"enabled": bool(entry.get("enabled", True)), "weight": weight}
        thresholds = entry.get("thresholds") or {}
        if thresholds:
            try:
                amber = int(thresholds.get("amber", 60))
                red = int(thresholds.get("red", 40))
            except (TypeError, ValueError):
                raise ConfigError(
                _("%(key)s: amber and red must be whole numbers.") % {"key": key}
            ) from None
            if not (0 <= red < amber <= 100):
                raise ConfigError(
                    _(
                        "%(key)s: thresholds must satisfy 0 ≤ red < amber ≤ 100 "
                        "(got red=%(red)s, amber=%(amber)s)."
                    )
                    % {"key": key, "red": red, "amber": amber}
                )
            item["thresholds"] = {"amber": amber, "red": red}
        out[key] = item
    return out


def _validate_notifications(value) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(_("Notifications configuration must be an object."))
    cid = value.get("eve_mail_sender_character_id")
    if cid in (None, "", "0", 0):
        sender = None
    else:
        try:
            sender = int(cid)
        except (TypeError, ValueError):
            raise ConfigError(_("EVE-mail sender must be a character id.")) from None
        if sender <= 0:
            raise ConfigError(_("EVE-mail sender must be a positive character id."))
    return {"eve_mail_sender_character_id": sender}


_VALIDATORS = {
    "dimensions": _validate_dimensions,
    "scoring": _validate_scoring,
    "responsibilities": _validate_responsibilities,
    "alerts": _validate_alerts,
    "finance": _validate_finance,
    "srp": _validate_srp,
    "recruitment": _validate_recruitment,
    "kpis": _validate_kpis,
    "notifications": _validate_notifications,
}
