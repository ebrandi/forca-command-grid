"""Admin Console: Industry & Economy settings (leadership-tunable defaults)."""
from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext_lazy as _

from apps.industry.models import IndustryEconomyConfig
from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

_INPUT = {"class": "input-field"}


class IndustryEconomyConfigForm(forms.ModelForm):
    class Meta:
        model = IndustryEconomyConfig
        fields = [
            "erp_redirects", "default_market_hub_system_id",
            "default_system_cost_index", "default_facility_tax",
            "default_sales_tax", "default_broker_fee",
            "corp_buyback_modifier", "hauling_cost_per_m3",
            "default_visibility", "allow_pilot_plans", "stale_price_hours",
            "consume_materials_on_delivery",
        ]
        widgets = {
            "default_market_hub_system_id": forms.NumberInput(attrs=_INPUT),
            "default_system_cost_index": forms.NumberInput(attrs={**_INPUT, "step": "0.0001"}),
            "default_facility_tax": forms.NumberInput(attrs={**_INPUT, "step": "0.0001"}),
            "default_sales_tax": forms.NumberInput(attrs={**_INPUT, "step": "0.0001"}),
            "default_broker_fee": forms.NumberInput(attrs={**_INPUT, "step": "0.0001"}),
            "corp_buyback_modifier": forms.NumberInput(attrs={**_INPUT, "step": "0.001"}),
            "hauling_cost_per_m3": forms.NumberInput(attrs={**_INPUT, "step": "0.01"}),
            "default_visibility": forms.Select(attrs=_INPUT),
            "stale_price_hours": forms.NumberInput(attrs=_INPUT),
        }


@login_required
@role_required(rbac.ROLE_OFFICER)
def industry_settings(request: HttpRequest) -> HttpResponse:
    """Configure market/tax/fee/facility defaults, visibility, and the /erp/ redirect."""
    config = IndustryEconomyConfig.active()
    if request.method == "POST":
        form = IndustryEconomyConfigForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            audit_log(
                request.user, "industry.economy.config", target_type="industry_config",
                target_id=str(config.pk), ip=client_ip(request),
            )
            messages.success(request, _("Industry & Economy settings saved."))
            return redirect("admin_audit:industry_settings")
        messages.error(request, _("Please correct the errors below."))
    else:
        form = IndustryEconomyConfigForm(instance=config)
    return render(request, "admin_audit/console/industry_settings.html",
                  {"form": form, "config": config})
