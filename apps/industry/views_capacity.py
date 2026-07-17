"""The Production Capacity board (P5) — officer views over manufacturing capacity.

Everything here is officer-only and audited. The page never invents numbers: it
renders what :mod:`apps.industry.capacity` (THE authority) derives — measured pools,
committed load, the unmeasured aggregate and the blocked-work panel — each figure
carrying an as-of label from the underlying sync cadence. A GET NEVER derives: the
page shows whatever resource rows exist with their staleness chips, and derivation
is an explicit, audited POST.

Named per-pilot capacity is shown to officers for pilots holding an active
``my_industry`` grant — a disclosed widening of that consent surface (the feature's
own description says so; see ``apps/sso/scopes.py``). Non-consenting pilots appear
only inside an anonymous count.
"""
from __future__ import annotations

import csv

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

from . import capacity
from .models import MrpConfig, ProductionResource

_INPUT = {"class": "input-field"}


class CapacitySettingsForm(forms.ModelForm):
    class Meta:
        model = MrpConfig
        fields = ["capacity_enabled", "capacity_skill_stale_days"]
        widgets = {
            "capacity_skill_stale_days": forms.NumberInput(attrs={**_INPUT, "min": "1"}),
        }


class ResourceOverrideForm(forms.ModelForm):
    class Meta:
        model = ProductionResource
        fields = [
            "manual_slots_override", "max_weekly_output",
            "unavailable_from", "unavailable_until", "is_paused",
        ]
        widgets = {
            "manual_slots_override": forms.NumberInput(attrs={**_INPUT, "min": "0"}),
            "max_weekly_output": forms.NumberInput(attrs={**_INPUT, "min": "0"}),
            "unavailable_from": forms.DateTimeInput(
                attrs={**_INPUT, "type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
            "unavailable_until": forms.DateTimeInput(
                attrs={**_INPUT, "type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
        }

    def clean(self):
        cleaned = super().clean()
        lo, hi = cleaned.get("unavailable_from"), cleaned.get("unavailable_until")
        if lo and hi and hi <= lo:
            raise forms.ValidationError(
                _("The maintenance window must end after it starts.")
            )
        return cleaned


def _pilot_rows(state: capacity.CapacityState) -> list[dict]:
    """Per-pilot detail across every activity class (consenting pilots only)."""
    now_day = capacity._day(timezone.now())
    resources = {
        r.pk: r for r in ProductionResource.objects.filter(
            pk__in=[p.resource_id for cls in capacity.ACTIVITY_CLASSES
                    for p in state.pools(cls) if p.resource_id]
        )
    }
    rows: list[dict] = []
    for cls in capacity.ACTIVITY_CLASSES:
        for pool in state.pools(cls):
            frees = capacity._initial_slot_free(pool, now_day)
            resource = resources.get(pool.resource_id)
            rows.append({
                "pool": pool,
                "activity_label": capacity._activity_label(cls),
                "next_free_at": min(frees) if frees else None,
                "override_form": ResourceOverrideForm(instance=resource) if resource else None,
            })
    return rows


def _pool_summary(state: capacity.CapacityState) -> list[dict]:
    return [
        {
            "activity_class": cls,
            "activity_label": capacity._activity_label(cls),
            "theoretical": state.theoretical(cls),
            "committed": state.committed(cls),
            "remaining": state.remaining_total(cls),
        }
        for cls in capacity.ACTIVITY_CLASSES
    ]


@login_required
@role_required(rbac.ROLE_OFFICER)
def capacity_board(request: HttpRequest) -> HttpResponse:
    """The Production Capacity board — pools, committed load, per-pilot detail,
    blocked work and the settings/override forms. Read-only GET (never derives)."""
    config = MrpConfig.active()
    state = capacity.capacity_state(config)
    if request.GET.get("export") == "csv":
        return _export_csv(state)

    from apps.industry.models import MrpRun

    last_run = MrpRun.objects.filter(status=MrpRun.Status.DONE).order_by("-started_at").first()
    return render(request, "industry/capacity.html", {
        "config": config,
        "settings_form": CapacitySettingsForm(instance=config),
        "pool_summary": _pool_summary(state),
        "pilot_rows": _pilot_rows(state),
        "by_location": capacity.committed_by_location(),
        "blocked": capacity.blocked_requirements(),
        "unmeasured_jobs": state.unmeasured_jobs,
        "unmatched_board_jobs": state.unmatched_board_jobs,
        "corp_jobs_as_of": state.corp_jobs_as_of,
        "char_jobs_as_of": state.char_jobs_as_of,
        "last_run": last_run,
        # Sync cadences shown beside the as-of labels (hours).
        "cadence_skills_h": 12,
        "cadence_corp_jobs_h": 3,
        "cadence_char_jobs_h": 6,
    })


def _export_csv(state: capacity.CapacityState) -> HttpResponse:
    """Per-pilot capacity as CSV (machine-stable English keys, the house convention)."""
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="production-capacity.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "character_id", "character_name", "activity_class", "slots",
        "used", "remaining", "paused", "as_of",
    ])
    for cls in capacity.ACTIVITY_CLASSES:
        for pool in state.pools(cls):
            writer.writerow([
                pool.character_id, pool.name, cls,
                "" if pool.effective_slots is None else pool.effective_slots,
                pool.used, "" if pool.remaining is None else pool.remaining,
                pool.is_paused,
                pool.as_of.isoformat() if pool.as_of else "",
            ])
    return response


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def capacity_settings(request: HttpRequest) -> HttpResponse:
    """Arm/disarm capacity planning and set the skill-staleness threshold."""
    config = MrpConfig.active()
    form = CapacitySettingsForm(request.POST, instance=config)
    if form.is_valid():
        form.save()
        audit_log(request.user, "industry.capacity.settings", target_type="mrp_config",
                  target_id=str(config.pk), ip=client_ip(request))
        messages.success(request, _("Capacity settings saved."))
    else:
        messages.error(request, _("Please correct the errors below."))
    return redirect("industry:capacity")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def capacity_resource(request: HttpRequest, pk: int) -> HttpResponse:
    """Save one pilot's officer overrides (slots / weekly cap / window / pause)."""
    resource = get_object_or_404(ProductionResource, pk=pk)
    form = ResourceOverrideForm(request.POST, instance=resource)
    if form.is_valid():
        form.save()
        audit_log(request.user, "industry.capacity.resource_override",
                  target_type="production_resource", target_id=str(resource.pk),
                  metadata={"character_id": resource.character_id,
                            "activity_class": resource.activity_class},
                  ip=client_ip(request))
        messages.success(request, _("Capacity override saved."))
    else:
        messages.error(request, _("Please correct the errors below."))
    return redirect("industry:capacity")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def capacity_derive(request: HttpRequest) -> HttpResponse:
    """Re-derive resource rows from current skills — the only on-demand derivation
    path (a GET never writes). Idempotent; audited."""
    config = MrpConfig.active()
    written = capacity.derive_resources(config)
    audit_log(request.user, "industry.capacity.derive", target_type="mrp_config",
              target_id=str(config.pk), metadata={"rows_written": written},
              ip=client_ip(request))
    messages.success(request, _("Capacity re-derived from current skills."))
    return redirect("industry:capacity")
