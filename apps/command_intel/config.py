"""Command Intelligence configuration (design doc 13).

Versioned ``admin_audit.AppSetting`` documents, one row per domain (key
``command_intel.<domain>``), read through this single access layer with code
``DEFAULTS``, a DEFAULTS-fingerprinted cache, validation, version-bump-on-write and
cache invalidation — the exact ``apps.readiness.config`` pattern. Providers and
admin never touch ``AppSetting`` directly.

Secrets live in env (``LLM_API_KEY`` etc.), never here. Every non-secret runtime
knob (model name, budgets, thresholds, minimisation flags, classification defaults)
is a domain below and is leadership-tunable without a deploy.
"""
from __future__ import annotations

import copy

_CACHE_TTL = 600
_VERSION_KEY = "command_intel._version"


class ConfigError(ValueError):
    """Raised on invalid config input, before any write (no partial writes)."""


# --- shipped defaults --------------------------------------------------------
# Every domain ships a complete default so CI works before any config exists.
DEFAULTS: dict[str, dict] = {
    # The LLM provider/runtime selection. The SECRET stays in env; this is the
    # non-secret runtime knobs only. Model NAMES here override the env default.
    "provider": {
        "model": "MiniMax-M2.7",               # MiniMax-M2.7 | MiniMax-M3
        "max_output_tokens": 8192,             # sized for reasoning(<think>) + answer
        "temperature": 0.3,
        "token_budget_per_report": 60000,      # hard cap on prompt+completion tokens
        "max_input_tokens": 48000,             # snapshot is compacted to fit under this
        "rate_limit_per_hour": 20,
        "rate_limit_per_day": 80,
        "monthly_token_ceiling": 500_000_000,  # safe fraction of the ~5.1B plan cap
        "request_timeout_s": 120,
        "retry_attempts": 2,                   # transport retries on 5xx/timeout
        "repair_attempts": 2,                  # schema/grounding re-prompts
        "template_model_overrides": {
            "deep_strategic_review": "MiniMax-M3",
        },
    },
    # Per-source enablement + data-minimisation flags (doc 04 §5).
    "sources": {
        "staleness_minutes": 15,
        # Privacy posture — leadership chose pseudonymisation for the non-EU/US provider.
        "pseudonymize_pilots": True,
        "include_named_pilots": False,
        "enabled": {},   # {source_key: bool}; absent ⇒ provider default_enabled
    },
    # Constraint engine thresholds + demand baseline (doc 05 §8).
    "constraints": {
        "global": {
            "critical_ratio": -0.10,
            "high_ratio": 0.05,
            "watch_ratio": 0.20,
            "forecast_window_days": 21,
        },
        "providers": {},          # {constraint_key: {"enabled": bool, ...}}
        "demand_targets": {},     # {"doctrine:<slug>": <pilots>} — seeded from data if empty
        "seed_demand_from_data": True,
    },
    # Impact estimation knobs (doc 09 §7).
    "impact": {
        "use_readiness_rerun": False,
        "confidence_weights": {"method": 0.4, "coverage": 0.3, "calibration": 0.3},
        "roi_cost_basis": "isk_then_effort",
        "training_eta_from_skills": True,
        "calibration_min_samples": 5,
    },
    # Course-of-Action generation rules (doc 07 §8).
    "coa_rules": {
        "max_coas_per_report": 8,
        "min_priority_to_surface": 30,
        "dedupe_against_recommendations": True,
        "dedupe_against_findings": True,
        "confidence_labels": {"high": 0.66, "medium": 0.4},
        "effort_default_hours": {"low": 2, "medium": 8, "high": 24},
        "auto_resolve_owner": True,
    },
    # Report templates (doc 06 §5, doc 13).
    "report_templates": {
        "default": "posture",
        "templates": {
            "posture": {
                "label": "Command Intelligence Report",
                "sections": [
                    "executive_summary", "operational_picture", "operational_constraints",
                    "courses_of_action", "strategic_risks", "forecast", "annexes",
                ],
                "default_classification": "high_command",
                "model_override": "",
            },
        },
    },
    # Prompt template version pinning (doc 12 §2).
    "prompts": {
        "active_version": 1,
        "system_preamble_override": "",
        "exemplars": [],
    },
    # Classification tier → minimum role rank (doc 14 §3). Enforced with a floor.
    "classification": {
        "default": "high_command",
        "tier_min_rank": {
            "corp_internal": "member",
            "high_command": "officer",
            "director_eyes_only": "director",
            "alliance_command": "director",
        },
    },
    # Officer responsibilities for COA assignment (reuse/extend readiness map).
    "responsibilities": {
        "owner_tags": {},     # {tag: [user_id, ...]}
    },
    # Scheduled-report delivery (P5, doc 14 §6 / doc 18). The cadence is the Celery
    # beat entry; this layer decides *whether* to generate on schedule and *where* a
    # ready report is announced. Delivery is classification-aware and ships DISARMED:
    # the weekly report generates, but nothing leaves the server until a director arms
    # a channel here (and configures a webhook / sender).
    "notifications": {
        "scheduled_enabled": True,          # the weekly beat generates a report
        "deliver_discord": False,           # arm to broadcast a classification-safe summary
        "deliver_evemail": False,           # arm to EVE-mail leadership
        # Which classifications MAY go to each channel. The validator refuses an unsafe
        # pairing — a corp-wide Discord channel can never carry the director/alliance tiers.
        "discord_classifications": ["corp_internal", "high_command"],
        "evemail_classifications": ["high_command", "director_eyes_only"],
        "evemail_sender_character_id": None,            # a corp character with esi-mail.send_mail.v1
        "evemail_owner_tags": ["strategic_director"],   # whose mains receive the targeted mail
        "min_severity_to_deliver": "watch",  # only announce when a binding constraint is at/above this
    },
    "campaign_policy": {
        "default_window_days": 21,
        "interaction_damping": 0.7,
        "trajectory_mode": "damped_sum",
        "complete_on": "target_or_all_milestones",
        "max_active_campaigns": 3,
    },
    # Autonomous COA proposal (P7, doc 17 §5). The kill switch is ``enabled`` (default
    # OFF): the beat fires but proposes nothing until a director arms it. Calibration-
    # gated — only proposes for action families whose measured-outcome history is
    # trustworthy (enough samples, tight error spread). It NEVER accepts a COA, creates a
    # task, or spends ISK — a human still commits every proposal.
    "autonomous": {
        "enabled": False,
        "min_calibration_samples": 5,   # a family needs >= this many measured outcomes
        "max_calibration_spread": 3.0,  # and error spread <= this to be trusted
        "max_proposals_per_run": 5,     # bound the proposals a single run may open
    },
    # Combat / battle intelligence (after-action reviews + combat Q&A over the killboard).
    "battle": {
        "analysis_classification": "high_command",  # AARs are officer-level by default
        "name_own_pilots": True,                     # name our own dead/primary (leadership choice)
        "respect_recognition_optout": True,          # ...but a member's opt-out always wins
        "recent_losses_days": 30,        # window for the /command/ask/ combat-context passages
        "recent_battles_scanned": 20,    # how many recent battle reports the retriever considers
        # CMD-1 (2.11): auto-AAR — a beat scans new battles and queues an AAR when one
        # crosses a threshold. Ships OFF (kill switch); respects the LLM rate caps and a
        # per-run cap so a busy night can't stampede the LLM budget.
        "auto_aar_enabled": False,
        "auto_aar_lookback_hours": 6,
        "auto_aar_max_per_run": 3,
        "auto_aar_min_isk_swing": 5_000_000_000,
        "auto_aar_min_our_losses": 5,
        "auto_aar_min_logi_lost": 1,
        "auto_aar_min_off_doctrine": 3,
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
    return f"command_intel.{domain}"


def _cache_key(domain: str) -> str:
    return f"command_intel:config:{domain}:{_DEFAULTS_FINGERPRINT}"


# --- read --------------------------------------------------------------------
def get(domain: str) -> dict:
    """Merged ``DEFAULTS[domain] ⊕ stored overrides``, deep-copied + cached."""
    if domain not in DEFAULTS:
        raise ConfigError(f"Unknown command_intel config domain: {domain!r}")
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
    """Monotonic counter bumped on every write; stamped into snapshots/reports."""
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


def _validate_provider(value: dict) -> dict:
    v = _ensure_dict(value)
    if "model" in v and not (isinstance(v["model"], str) and v["model"].strip()):
        raise ConfigError("provider.model must be a non-empty string")
    mot = v.get("max_output_tokens")
    if mot is not None and not (isinstance(mot, int) and 256 <= mot <= 65536):
        raise ConfigError("provider.max_output_tokens must be an int in [256, 65536]")
    temp = v.get("temperature")
    if temp is not None and not (isinstance(temp, int | float) and 0 <= temp <= 1):
        raise ConfigError("provider.temperature must be in [0, 1]")
    return v


def _validate_classification(value: dict) -> dict:
    v = _ensure_dict(value)
    ranks = {"public", "member", "officer", "director", "admin"}
    floor = {"corp_internal": "member", "high_command": "officer",
             "director_eyes_only": "director", "alliance_command": "director"}
    mapping = v.get("tier_min_rank", {})
    for tier, min_floor in floor.items():
        got = mapping.get(tier, min_floor)
        if got not in ranks:
            raise ConfigError(f"classification.tier_min_rank.{tier} invalid: {got!r}")
        # Hard floor: never allow a tier to be more permissive than its design floor.
        order = {"public": 0, "member": 10, "officer": 20, "director": 30, "admin": 40}
        if order[got] < order[min_floor]:
            raise ConfigError(
                f"classification.tier_min_rank.{tier} cannot be below {min_floor!r}"
            )
    return v


_VALID_TIERS = {"corp_internal", "high_command", "director_eyes_only", "alliance_command"}
# Tiers a corp-wide Discord broadcast must NEVER carry (doc 14 §6: the access gate is
# right but a broadcast would spill the content). EVE-mail is targeted, so it may.
_BROADCAST_FORBIDDEN = {"director_eyes_only", "alliance_command"}


def _validate_notifications(value: dict) -> dict:
    """Guard the delivery routing — the unsafe-pairing refusal of doc 14 §6."""
    v = _ensure_dict(value)
    disc = v.get("discord_classifications")
    if disc is not None:
        if not isinstance(disc, list) or any(c not in _VALID_TIERS for c in disc):
            raise ConfigError(
                "notifications.discord_classifications must be a list of valid classification tiers"
            )
        # NB: the module-level ``set()`` config writer shadows the builtin here, so use
        # the set method rather than ``set(disc)``.
        unsafe = _BROADCAST_FORBIDDEN.intersection(disc)
        if unsafe:
            raise ConfigError(
                "notifications.discord_classifications cannot include broadcast-forbidden "
                f"tier(s): {sorted(unsafe)}"
            )
    mail = v.get("evemail_classifications")
    if mail is not None and (not isinstance(mail, list) or any(c not in _VALID_TIERS for c in mail)):
        raise ConfigError(
            "notifications.evemail_classifications must be a list of valid classification tiers"
        )
    sender = v.get("evemail_sender_character_id")
    if sender is not None and (isinstance(sender, bool) or not isinstance(sender, int) or sender <= 0):
        raise ConfigError("notifications.evemail_sender_character_id must be a positive integer or null")
    return v


def _validate_autonomous(value: dict) -> dict:
    """Guard the autonomous-proposer thresholds (the kill switch itself is a plain bool)."""
    v = _ensure_dict(value)
    # A family must have at least ONE measured outcome to be trusted — 0 would make every
    # family trusted with no track record, defeating the calibration gate.
    if "min_calibration_samples" in v:
        n = v["min_calibration_samples"]
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise ConfigError("autonomous.min_calibration_samples must be an integer >= 1")
    if "max_proposals_per_run" in v:
        n = v["max_proposals_per_run"]
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise ConfigError("autonomous.max_proposals_per_run must be a non-negative integer")
    if "max_calibration_spread" in v:
        s = v["max_calibration_spread"]
        if isinstance(s, bool) or not isinstance(s, int | float) or s < 0:
            raise ConfigError("autonomous.max_calibration_spread must be a non-negative number")
    return v


def _validate_battle(value: dict) -> dict:
    """Guard the battle-analysis classification (naming flags are plain bools) and coerce
    the auto-AAR knobs to safe types so a direct set can't store a value that breaks the beat."""
    v = _ensure_dict(value)
    cls = v.get("analysis_classification")
    if cls is not None and cls not in _VALID_TIERS:
        raise ConfigError("battle.analysis_classification must be a valid classification tier")
    if "auto_aar_enabled" in v:
        v["auto_aar_enabled"] = bool(v["auto_aar_enabled"])
    # Non-negative ints; the two scan-scope knobs are also upper-bounded so a direct set
    # (bypassing the UI clamps) can't widen the scan window / per-run batch unboundedly.
    _ceilings = {"auto_aar_lookback_hours": 168, "auto_aar_max_per_run": 50}
    for key in (
        "auto_aar_lookback_hours", "auto_aar_max_per_run", "auto_aar_min_isk_swing",
        "auto_aar_min_our_losses", "auto_aar_min_logi_lost", "auto_aar_min_off_doctrine",
    ):
        if key in v:
            try:
                coerced = max(0, int(v[key]))
            except (TypeError, ValueError):
                raise ConfigError(f"battle.{key} must be a non-negative integer") from None
            ceiling = _ceilings.get(key)
            v[key] = min(coerced, ceiling) if ceiling else coerced
    return v


_VALIDATORS: dict[str, callable] = {
    "provider": _validate_provider,
    "classification": _validate_classification,
    "notifications": _validate_notifications,
    "autonomous": _validate_autonomous,
    "battle": _validate_battle,
    # Remaining domains accept any well-formed object (structural defaults guard them).
    "sources": _ensure_dict,
    "constraints": _ensure_dict,
    "impact": _ensure_dict,
    "coa_rules": _ensure_dict,
    "report_templates": _ensure_dict,
    "prompts": _ensure_dict,
    "responsibilities": _ensure_dict,
    "campaign_policy": _ensure_dict,
}


# --- write -------------------------------------------------------------------
def set(domain: str, value: dict, *, user=None) -> dict:  # noqa: A001 - documented public API name
    """Validate + persist a config document, bump the version, bust the cache.

    Raises :class:`ConfigError` before any write. The caller records the audit trail.
    """
    if domain not in DEFAULTS:
        raise ConfigError(f"Unknown command_intel config domain: {domain!r}")
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
        raise ConfigError(f"Unknown command_intel config domain: {domain!r}")
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
