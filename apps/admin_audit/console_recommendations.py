"""Admin Console: designated notification/mail relay character (REC-1 / 2.10)."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render

from apps.recommendations import relay
from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def relay_settings(request: HttpRequest) -> HttpResponse:
    """Pick / rotate the corp character whose in-game notifications + mail FORCA relays."""
    eligible = relay.eligible_relay_characters()
    eligible_ids = {c["character_id"] for c in eligible}

    if request.method == "POST":
        raw = (request.POST.get("character_id") or "").strip()
        if raw == "":
            relay.set_designated_relay_character(None)
            audit_log(request.user, "recommendations.relay.clear", target_type="corp",
                      target_id="relay", ip=client_ip(request))
            messages.success(request, "Relay character cleared — falling back to the first valid token.")
            return redirect("admin_audit:relay_settings")
        try:
            cid = int(raw)
        except ValueError:
            messages.error(request, "Invalid character.")
            return redirect("admin_audit:relay_settings")
        # Only allow a character that actually holds a relay scope (never trust the POST id).
        if cid not in eligible_ids:
            messages.error(request, "That character does not currently hold a relay scope.")
            return redirect("admin_audit:relay_settings")
        relay.set_designated_relay_character(cid)
        audit_log(request.user, "recommendations.relay.set", target_type="character",
                  target_id=str(cid), ip=client_ip(request))
        messages.success(request, "Designated relay character saved.")
        return redirect("admin_audit:relay_settings")

    return render(request, "admin_audit/console/relay_settings.html", {
        "eligible": eligible,
        "current": relay.designated_relay_character_id(),
    })


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def recommendations_tuning(request: HttpRequest) -> HttpResponse:
    """REC-2 (2.13): enable/disable evaluators, tune the combat-loss window/threshold, and
    set a severity floor — no deploy required."""
    from apps.recommendations.engine import EVALUATOR_REGISTRY
    from apps.recommendations.models import RecommendationConfig

    cfg = RecommendationConfig.active()
    if request.method == "POST":
        enabled = set(request.POST.getlist("evaluator"))
        cfg.disabled_evaluators = [k for k, _label, _f in EVALUATOR_REGISTRY if k not in enabled]
        cfg.combat_loss_window_days = max(1, min(90, _int(request.POST.get("combat_loss_window_days"), 7)))
        cfg.combat_loss_threshold = max(1, min(100, _int(request.POST.get("combat_loss_threshold"), 3)))
        cfg.min_severity = max(0, min(100, _int(request.POST.get("min_severity"), 0)))
        cfg.save()
        audit_log(request.user, "recommendations.tuning.saved",
                  target_type="recommendation_config", target_id=str(cfg.pk), ip=client_ip(request))
        messages.success(request, "Recommendation engine tuning saved.")
        return redirect("admin_audit:recommendations_tuning")

    disabled = set(cfg.disabled_evaluators or [])
    evaluators = [
        {"key": k, "label": label, "enabled": k not in disabled}
        for k, label, _f in EVALUATOR_REGISTRY
    ]
    return render(request, "admin_audit/console/recommendations_tuning.html",
                  {"cfg": cfg, "evaluators": evaluators})
