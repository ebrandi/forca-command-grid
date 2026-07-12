"""Command Intelligence configuration pages (admin console, Director-gated).

Per design doc 13, CI *configuration* lives in the ``admin_audit:`` namespace and
reuses the readiness console contract verbatim — ``@login_required`` +
``@role_required(ROLE_DIRECTOR)``, render to
``templates/admin_audit/console/command_intel/*.html``, and on every write funnel
through the single ``apps.command_intel.config`` writer (validate → persist →
version-bump → cache-bust) then ``audit_log``. The LLM **secret** is never shown,
edited or round-tripped through any form — pages read only ``COMMAND_INTEL_ENABLED``.
"""
from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.command_intel import config
from apps.command_intel.engine import registry
from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required


def _int_or(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_or(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _num_or(value, default=None):
    """Parse a numeric token as int when whole, else float (demand targets)."""
    f = _float_or(value)
    if f is None:
        return default
    return int(f) if f == int(f) else f


# --- shared write path (one audited trail per write) -------------------------
def _audited_set(request: HttpRequest, domain: str, doc: dict, *, ok_message: str, back: str) -> HttpResponse:
    """Validate+persist a config document, audit it, and redirect — the common path."""
    try:
        config.set(domain, doc, user=request.user)
    except config.ConfigError as exc:
        # No partial write: the stored doc and version are untouched.
        messages.error(request, str(exc))
        return redirect(back)
    audit_log(request.user, "command_intel.config.update",
              target_type="command_intel_config", target_id=domain,
              metadata={"domain": domain}, ip=client_ip(request))
    messages.success(request, ok_message)
    return redirect(back)


# Domains that have an admin page to redirect back to after a reset.
_RESET_RETURN = {
    "provider": "admin_audit:command_intel_provider",
    "constraints": "admin_audit:command_intel_constraints",
    "classification": "admin_audit:command_intel_classification",
    "notifications": "admin_audit:command_intel_notifications",
    "autonomous": "admin_audit:command_intel_autonomous",
    "battle": "admin_audit:command_intel_auto_aar",
}


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def command_intel_reset(request: HttpRequest, domain: str) -> HttpResponse:
    """Restore one CI config domain to its code defaults (a single audited ``reset``)."""
    back = _RESET_RETURN.get(domain, "admin_audit:command_intel_provider")
    try:
        config.reset(domain, user=request.user)
    except config.ConfigError as exc:
        messages.error(request, str(exc))
        return redirect(back)
    audit_log(request.user, "command_intel.config.reset",
              target_type="command_intel_config", target_id=domain,
              metadata={"domain": domain}, ip=client_ip(request))
    messages.success(request, _("%(domain)s reset to defaults.") % {"domain": domain.title()})
    return redirect(back)


# --- Provider & model (config domain "provider"; doc 13 §2.1) ----------------
# The SECRET (LLM_API_KEY) never appears here; only the non-secret runtime knobs.
_PROVIDER_INT_FIELDS = ["max_output_tokens", "token_budget_per_report",
                        "monthly_token_ceiling", "rate_limit_per_hour", "repair_attempts"]


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def command_intel_provider(request: HttpRequest) -> HttpResponse:
    """Pick the runtime model, budgets and call discipline. The API key is never shown."""
    if request.method == "POST":
        current = config.get("provider")
        doc = dict(current)
        model = (request.POST.get("model") or "").strip()
        if model:
            doc["model"] = model
        for key in _PROVIDER_INT_FIELDS:
            doc[key] = _int_or(request.POST.get(key), current.get(key))
        doc["temperature"] = _float_or(request.POST.get("temperature"), current.get("temperature"))
        return _audited_set(request, "provider", doc,
                            ok_message=_("Provider & model settings saved."),
                            back="admin_audit:command_intel_provider")
    return render(request, "admin_audit/console/command_intel/provider.html", {
        "enabled": bool(getattr(settings, "COMMAND_INTEL_ENABLED", False)),
        "cfg": config.get("provider"),
        "meta": config.meta("provider"),
    })


# --- Constraints & thresholds (config domain "constraints"; doc 13 §2.3) -----
_GLOBAL_RATIOS = [
    ("critical_ratio", _("Critical ratio"), _("Headroom/demand at or below this colours a constraint critical.")),
    ("high_ratio", _("High ratio"), _("Above critical, at or below this colours it high.")),
    ("watch_ratio", _("Watch ratio"), _("Above high, at or below this colours it watch.")),
]


def _provider_rows(cfg: dict) -> list[dict]:
    """Each registered constraint provider joined to its enable flag, in registry order."""
    providers = cfg.get("providers") or {}
    rows = []
    for p in registry.constraints():
        entry = providers.get(p.key, {})
        rows.append({
            "key": p.key,
            "label": getattr(p, "label", p.key.title()),
            "category": getattr(p, "category", ""),
            "enabled": entry.get("enabled", getattr(p, "default_enabled", True)),
        })
    return rows


def _demand_text(cfg: dict) -> str:
    """Render demand_targets ({key: pilots}) as one ``key value`` line per entry."""
    targets = cfg.get("demand_targets") or {}
    return "\n".join(f"{k} {v}" for k, v in targets.items())


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def command_intel_constraints(request: HttpRequest) -> HttpResponse:
    """Edit global severity ratios, demand targets and per-provider enable toggles."""
    if request.method == "POST":
        current = config.get("constraints")
        glob = dict(current.get("global") or {})
        for key, _label, _help in _GLOBAL_RATIOS:
            glob[key] = _float_or(request.POST.get(key), glob.get(key))
        providers = dict(current.get("providers") or {})
        for p in registry.constraints():
            entry = dict(providers.get(p.key) or {})
            entry["enabled"] = request.POST.get(f"provider_{p.key}_enabled") == "on"
            providers[p.key] = entry
        demand: dict[str, float] = {}
        for line in (request.POST.get("demand_targets") or "").splitlines():
            parts = line.replace("=", " ").split()
            if len(parts) < 2:
                continue
            val = _num_or(parts[-1])
            if val is not None:
                demand[parts[0]] = val
        doc = dict(current)
        doc["global"] = glob
        doc["providers"] = providers
        doc["demand_targets"] = demand
        return _audited_set(request, "constraints", doc,
                            ok_message=_("Constraints & thresholds saved."),
                            back="admin_audit:command_intel_constraints")
    cfg = config.get("constraints")
    glob = cfg.get("global") or {}
    return render(request, "admin_audit/console/command_intel/constraints.html", {
        "ratios": [{"key": k, "label": label, "help": help_, "value": glob.get(k, "")}
                   for k, label, help_ in _GLOBAL_RATIOS],
        "providers": _provider_rows(cfg),
        "demand_text": _demand_text(cfg),
        "meta": config.meta("constraints"),
    })


# --- Classification (config domain "classification"; doc 13 §2.8) ------------
# The floor is enforced server-side by the validator; the page only offers ranks.
_RANKS = ["public", "member", "officer", "director", "admin"]


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def command_intel_classification(request: HttpRequest) -> HttpResponse:
    """Set the default classification and the tier → minimum-role map (floor-guarded)."""
    if request.method == "POST":
        current = config.get("classification")
        tiers = current.get("tier_min_rank") or {}
        new_tiers: dict[str, str] = {}
        for tier, cur_rank in tiers.items():
            chosen = request.POST.get(f"tier_{tier}")
            new_tiers[tier] = chosen if chosen in _RANKS else cur_rank
        default = request.POST.get("default") or current.get("default")
        doc = dict(current)
        doc["default"] = default
        doc["tier_min_rank"] = new_tiers
        return _audited_set(request, "classification", doc,
                            ok_message=_("Classification settings saved."),
                            back="admin_audit:command_intel_classification")
    cfg = config.get("classification")
    tiers = cfg.get("tier_min_rank") or {}
    return render(request, "admin_audit/console/command_intel/classification.html", {
        "default": cfg.get("default", ""),
        "tiers": [{"key": tier, "label": tier.replace("_", " ").title(), "rank": rank}
                  for tier, rank in tiers.items()],
        "tier_keys": list(tiers.keys()),
        "ranks": _RANKS,
        "meta": config.meta("classification"),
    })


# --- Notifications / scheduled delivery (config domain "notifications"; doc 14 §6) ---
# Only broadcast-SAFE tiers are offered for Discord; the config validator also refuses
# the forbidden pairing server-side, so the page can never arm an unsafe broadcast.
_TIER_LABELS = {
    "corp_internal": _("Corporation Internal"),
    "high_command": _("High Command"),
    "director_eyes_only": _("Director — Eyes Only"),
    "alliance_command": _("Alliance Command"),
}
_DISCORD_TIERS = ["corp_internal", "high_command"]
_MAIL_TIERS = ["corp_internal", "high_command", "director_eyes_only", "alliance_command"]
_SEVERITIES = ["info", "watch", "high", "critical"]


def _tier_options(selected: list, tiers: list[str]) -> list[dict]:
    chosen = set(selected or [])
    return [{"value": t, "label": _TIER_LABELS[t], "checked": t in chosen} for t in tiers]


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def command_intel_notifications(request: HttpRequest) -> HttpResponse:
    """Arm scheduled-report delivery and route it per classification (ships disarmed)."""
    if request.method == "POST":
        current = config.get("notifications")
        doc = dict(current)
        doc["scheduled_enabled"] = request.POST.get("scheduled_enabled") == "on"
        doc["deliver_discord"] = request.POST.get("deliver_discord") == "on"
        doc["deliver_evemail"] = request.POST.get("deliver_evemail") == "on"
        doc["discord_classifications"] = [
            t for t in request.POST.getlist("discord_classifications") if t in _DISCORD_TIERS
        ]
        doc["evemail_classifications"] = [
            t for t in request.POST.getlist("evemail_classifications") if t in _MAIL_TIERS
        ]
        sender = _int_or(request.POST.get("evemail_sender_character_id"))
        doc["evemail_sender_character_id"] = sender if sender else None
        doc["evemail_owner_tags"] = (request.POST.get("evemail_owner_tags") or "").replace(",", " ").split()
        sev = request.POST.get("min_severity_to_deliver")
        if sev in _SEVERITIES:
            doc["min_severity_to_deliver"] = sev
        return _audited_set(request, "notifications", doc,
                            ok_message=_("Notification & delivery settings saved."),
                            back="admin_audit:command_intel_notifications")
    cfg = config.get("notifications")
    return render(request, "admin_audit/console/command_intel/notifications.html", {
        "cfg": cfg,
        "enabled": bool(getattr(settings, "COMMAND_INTEL_ENABLED", False)),
        "discord_options": _tier_options(cfg.get("discord_classifications"), _DISCORD_TIERS),
        "mail_options": _tier_options(cfg.get("evemail_classifications"), _MAIL_TIERS),
        "severities": _SEVERITIES,
        "owner_tags_text": " ".join(cfg.get("evemail_owner_tags") or []),
        "meta": config.meta("notifications"),
    })


# --- Autonomous proposal (config domain "autonomous"; doc 17 §5) --------------
def _calibration_rows() -> list[dict]:
    """Per-family calibration + whether it currently clears the autonomous trust gate."""
    from apps.command_intel import outcomes

    cfg = config.get("autonomous")
    min_n = int(cfg.get("min_calibration_samples", 5))
    max_spread = float(cfg.get("max_calibration_spread", 3.0))
    return [
        {**cal, "trusted": cal["n"] >= min_n and cal["spread"] <= max_spread}
        for cal in outcomes.calibration_summary()
    ]


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def command_intel_autonomous(request: HttpRequest) -> HttpResponse:
    """Arm/disarm the autonomous proposer (the kill switch) + its calibration gate."""
    if request.method == "POST":
        current = config.get("autonomous")
        doc = dict(current)
        doc["enabled"] = request.POST.get("enabled") == "on"
        doc["min_calibration_samples"] = _int_or(
            request.POST.get("min_calibration_samples"), current.get("min_calibration_samples"))
        doc["max_proposals_per_run"] = _int_or(
            request.POST.get("max_proposals_per_run"), current.get("max_proposals_per_run"))
        doc["max_calibration_spread"] = _float_or(
            request.POST.get("max_calibration_spread"), current.get("max_calibration_spread"))
        return _audited_set(request, "autonomous", doc,
                            ok_message=_("Autonomous proposal settings saved."),
                            back="admin_audit:command_intel_autonomous")
    return render(request, "admin_audit/console/command_intel/autonomous.html", {
        "cfg": config.get("autonomous"),
        "enabled": bool(getattr(settings, "COMMAND_INTEL_ENABLED", False)),
        "calibration": _calibration_rows(),
        "meta": config.meta("autonomous"),
    })


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def command_intel_auto_aar(request: HttpRequest) -> HttpResponse:
    """CMD-1 (2.11): arm/disarm auto-AAR and set the notability thresholds. Ships OFF."""
    cfg = config.get("battle")
    if request.method == "POST":
        doc = dict(cfg)  # preserve the other battle settings; edit only the auto-AAR knobs
        doc["auto_aar_enabled"] = request.POST.get("auto_aar_enabled") == "on"
        doc["auto_aar_lookback_hours"] = min(168, max(1, _int_or(request.POST.get("auto_aar_lookback_hours"), 6)))
        doc["auto_aar_max_per_run"] = min(20, max(1, _int_or(request.POST.get("auto_aar_max_per_run"), 3)))
        doc["auto_aar_min_isk_swing"] = max(0, _int_or(request.POST.get("auto_aar_min_isk_swing"), 5_000_000_000))
        doc["auto_aar_min_our_losses"] = max(0, _int_or(request.POST.get("auto_aar_min_our_losses"), 5))
        doc["auto_aar_min_logi_lost"] = max(0, _int_or(request.POST.get("auto_aar_min_logi_lost"), 1))
        doc["auto_aar_min_off_doctrine"] = max(0, _int_or(request.POST.get("auto_aar_min_off_doctrine"), 3))
        return _audited_set(request, "battle", doc, ok_message=_("Auto-AAR settings saved."),
                            back="admin_audit:command_intel_auto_aar")
    return render(request, "admin_audit/console/command_intel/auto_aar.html",
                  {"cfg": cfg, "meta": config.meta("battle")})
