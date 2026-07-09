"""Admin Console: Jump Planner settings (leadership-tunable defaults)."""
from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render

from apps.navigation.models import JumpPlannerConfig
from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

_INPUT = {"class": "input-field"}
_LEVELS = [(n, str(n)) for n in range(6)]


class JumpPlannerConfigForm(forms.ModelForm):
    default_jdc = forms.TypedChoiceField(choices=_LEVELS, coerce=int, widget=forms.Select(attrs=_INPUT))
    default_jfc = forms.TypedChoiceField(choices=_LEVELS, coerce=int, widget=forms.Select(attrs=_INPUT))
    default_jf_skill = forms.TypedChoiceField(choices=_LEVELS, coerce=int, widget=forms.Select(attrs=_INPUT))

    class Meta:
        model = JumpPlannerConfig
        fields = [
            "enabled", "default_jdc", "default_jfc", "default_jf_skill",
            "prefer_stations", "default_preference", "fuel_safety_margin_pct",
            "avoid_systems", "avoid_regions", "allow_pilot_exit_override",
            "allow_saved_routes", "highsec_exit_warning",
        ]
        widgets = {
            "default_preference": forms.Select(attrs=_INPUT),
            "fuel_safety_margin_pct": forms.NumberInput(attrs={**_INPUT, "step": "0.5", "min": "0"}),
            "avoid_systems": forms.Textarea(attrs={**_INPUT, "rows": 2}),
            "avoid_regions": forms.Textarea(attrs={**_INPUT, "rows": 2}),
            "highsec_exit_warning": forms.Textarea(attrs={**_INPUT, "rows": 3}),
        }

    def clean_fuel_safety_margin_pct(self):
        val = self.cleaned_data["fuel_safety_margin_pct"]
        if val < 0 or val > 100:
            raise forms.ValidationError("Safety margin must be between 0 and 100%.")
        return val


@login_required
@role_required(rbac.ROLE_OFFICER)
def jump_planner_settings(request: HttpRequest) -> HttpResponse:
    """Configure Jump Planner defaults: skill assumptions, exit strategy, avoid-lists."""
    config = JumpPlannerConfig.active()
    if request.method == "POST":
        form = JumpPlannerConfigForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            audit_log(
                request.user, "navigation.jump_planner.config",
                target_type="jump_planner_config", target_id=str(config.pk),
                ip=client_ip(request),
            )
            messages.success(request, "Jump Planner settings saved.")
            return redirect("admin_audit:jump_planner_settings")
        messages.error(request, "Please correct the errors below.")
    else:
        form = JumpPlannerConfigForm(instance=config)
    return render(request, "admin_audit/console/jump_planner_settings.html",
                  {"form": form, "config": config})
