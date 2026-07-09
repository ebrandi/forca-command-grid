"""Admin Console: Corporation infrastructure alert thresholds (CORP-3 / 2.3)."""
from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render

from apps.corporation.models import StructureAlertConfig
from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

_INPUT = {"class": "input-field"}


class StructureAlertConfigForm(forms.ModelForm):
    class Meta:
        model = StructureAlertConfig
        fields = ["fuel_alert_days", "adm_alert_floor"]
        widgets = {
            "fuel_alert_days": forms.NumberInput(attrs={**_INPUT, "min": "1", "max": "30"}),
            "adm_alert_floor": forms.NumberInput(attrs={**_INPUT, "step": "0.1", "min": "1", "max": "6"}),
        }

    def clean_fuel_alert_days(self):
        v = self.cleaned_data["fuel_alert_days"]
        # Bound server-side too (the widget min/max is client-only): a crafted 0 would
        # make is_low_fuel always false and silently disable the whole fuel alert.
        if not (1 <= v <= 30):
            raise forms.ValidationError("Low-fuel warning must be between 1 and 30 days.")
        return v

    def clean_adm_alert_floor(self):
        v = self.cleaned_data["adm_alert_floor"]
        if not (1.0 <= v <= 6.0):
            raise forms.ValidationError("ADM floor must be between 1.0 and 6.0.")
        return v


@login_required
@role_required(rbac.ROLE_OFFICER)
def structure_alert_settings(request: HttpRequest) -> HttpResponse:
    """Set the fuel-days / sov-ADM thresholds that drive the board flags and the alert."""
    config = StructureAlertConfig.active()
    if request.method == "POST":
        form = StructureAlertConfigForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            audit_log(
                request.user, "corporation.structure_alerts.config",
                target_type="structure_alert_config", target_id=str(config.pk),
                ip=client_ip(request),
            )
            messages.success(request, "Structure-alert thresholds saved.")
            return redirect("admin_audit:structure_alert_settings")
        messages.error(request, "Please correct the errors below.")
    else:
        form = StructureAlertConfigForm(instance=config)
    return render(request, "admin_audit/console/structure_alerts.html",
                  {"form": form, "config": config})
