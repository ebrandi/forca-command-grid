"""Capsuleer Path admin console (director-gated, doc 10 §4, brief §9).

Follows the console contract exactly — ``@login_required`` + ``@role_required(ROLE_DIRECTOR)``,
render to ``templates/admin_audit/console/capsuleer.html``, and on every write go through
``apps.capsuleer.config.set`` (validate → version-bump → cache-bust) then ``audit_log``. Exposes the
brief §9 knobs: reconcile/suggestions enable, the suggestion open-cap, the leadership min-group floor,
retention windows, the per-event notification arm map, and the disabled built-in template list.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

_CONFIG_DOMAINS = ("reconcile", "suggestions", "leadership", "retention", "templates",
                   "notifications")


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def capsuleer_console(request: HttpRequest) -> HttpResponse:
    from apps.capsuleer import config
    from apps.capsuleer.templates_builtin import BUILTIN

    armed = config.get("notifications").get("enabled", {})
    ctx = {
        "reconcile": config.get("reconcile"),
        "suggestions": config.get("suggestions"),
        "leadership": config.get("leadership"),
        "retention": config.get("retention"),
        "templates_cfg": config.get("templates"),
        "builtin_templates": [{"key": t["key"], "name": t["name"]} for t in BUILTIN],
        "notification_events": [
            {"key": ev, "armed": bool(armed.get(ev))}
            for ev in ("milestone_reached", "goal_completed", "review_due", "suggestion")
        ],
        "meta": config.meta("suggestions"),
    }
    return render(request, "admin_audit/console/capsuleer.html", ctx)


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def capsuleer_save(request: HttpRequest) -> HttpResponse:
    from apps.capsuleer import config

    p = request.POST
    if p.get("action") == "reset":
        for domain in _CONFIG_DOMAINS:
            config.reset(domain, user=request.user)
        audit_log(request.user, "capsuleer.config.reset", target_type="capsuleer_config",
                  ip=client_ip(request))
        messages.success(request, "Capsuleer Path settings reset to defaults.")
        return redirect("admin_audit:capsuleer_console")
    try:
        config.set("reconcile", {"enabled": p.get("reconcile_enabled") == "on"}, user=request.user)
        config.set("suggestions", {
            "enabled": p.get("suggestions_enabled") == "on",
            "max_open_per_user": _int(p.get("max_open_per_user"), 6),
        }, user=request.user)
        config.set("leadership", {"min_group": _int(p.get("min_group"), 4)}, user=request.user)
        config.set("retention", {
            "snapshots_days": _int(p.get("snapshots_days"), 400),
            "suggestions_days": _int(p.get("suggestions_days"), 60),
            "activity_days": _int(p.get("activity_days"), 365),
        }, user=request.user)
        config.set("notifications", {"enabled": {
            ev: p.get(f"notify_{ev}") == "on"
            for ev in ("milestone_reached", "goal_completed", "review_due", "suggestion")
        }}, user=request.user)
        disabled = [k for k in p.getlist("disabled_keys") if k]
        config.set("templates", {"disabled_keys": disabled}, user=request.user)
    except config.ConfigError as exc:
        messages.error(request, str(exc))
        return redirect("admin_audit:capsuleer_console")
    audit_log(request.user, "capsuleer.config.update", target_type="capsuleer_config",
              ip=client_ip(request))
    messages.success(request, "Capsuleer Path settings saved.")
    return redirect("admin_audit:capsuleer_console")


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
