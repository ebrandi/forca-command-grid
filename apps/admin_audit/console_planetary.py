"""Admin Console: Planetary Industry governance.

Leadership tunes the corp-wide PI defaults (market hub, taxes, buyback, hauling,
priority products) and reviews shared plans. Config edits are Director-gated; the
overview is officer-visible. Mirrors the console conventions: audit_log + PRG.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render

from apps.planetary import services
from apps.planetary.forms import PlanetaryConfigForm
from apps.planetary.models import PiMaterial, PiPlan, PiStatus
from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required


def _audit(request, action, **kw):
    audit_log(request.user, action, ip=client_ip(request), **kw)


@login_required
@role_required(rbac.ROLE_OFFICER)
def planetary_hub(request: HttpRequest) -> HttpResponse:
    config = services.active_config()
    plans = PiPlan.objects.all()
    shared = list(
        services.shared_plans(request.user)
        .select_related("owner").prefetch_related("planets")[:25]
    )
    rows = []
    for p in shared:
        totals = (p.snapshot or {}).get("totals", {})
        rows.append({
            "plan": p,
            "owner": p.owner.get_username(),
            "net_day": totals.get("net_day"),
            "planets": p.planets.count(),
        })
    priority = list(PiMaterial.objects.filter(type_id__in=config.recommended_products))
    return render(request, "admin_audit/console/planetary/hub.html", {
        "config": config,
        "total_plans": plans.exclude(status=PiStatus.ARCHIVED).count(),
        "active_plans": plans.filter(status=PiStatus.ACTIVE).count(),
        "shared": rows,
        "priority": priority,
    })


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def planetary_config(request: HttpRequest) -> HttpResponse:
    config = services.active_config()
    if request.method == "POST":
        form = PlanetaryConfigForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            _audit(request, "planetary.config_update", target_type="planetary_config",
                   target_id=str(config.pk))
            messages.success(request, "Planetary Industry settings saved.")
            return redirect("admin_audit:planetary_config")
        messages.error(request, "Please correct the errors below.")
    else:
        form = PlanetaryConfigForm(instance=config)
    return render(request, "admin_audit/console/planetary/config.html",
                  {"form": form, "config": config})
