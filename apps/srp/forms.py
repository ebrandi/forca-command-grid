"""Forms for the leadership SRP settings page (programme + rule editing)."""
from __future__ import annotations

from decimal import Decimal

from django import forms

from apps.doctrines.models import Doctrine

from .models import SrpProgram, SrpRule

_INPUT = {"class": "input-field"}
_ISK = {**_INPUT, "step": "1", "min": "0"}


class SrpProgramForm(forms.ModelForm):
    class Meta:
        model = SrpProgram
        fields = [
            "enabled", "payout_mode", "valuation", "default_cap",
            "require_doctrine", "cover_pod",
            "require_fleet_op", "fleet_op_grace_minutes",
            "fleet_op_default_duration_minutes", "fleet_op_require_attendance",
            "auto_draft_enabled",
            "insurance_fraction", "intro_text",
        ]
        widgets = {
            "enabled": forms.CheckboxInput(attrs={"class": "h-4 w-4"}),
            "auto_draft_enabled": forms.CheckboxInput(attrs={"class": "h-4 w-4"}),
            "payout_mode": forms.Select(attrs=_INPUT),
            "valuation": forms.Select(attrs=_INPUT),
            "default_cap": forms.NumberInput(attrs=_ISK),
            "require_doctrine": forms.CheckboxInput(attrs={"class": "h-4 w-4"}),
            "cover_pod": forms.CheckboxInput(attrs={"class": "h-4 w-4"}),
            "require_fleet_op": forms.CheckboxInput(attrs={"class": "h-4 w-4"}),
            "fleet_op_grace_minutes": forms.NumberInput(attrs={**_INPUT, "min": "0"}),
            "fleet_op_default_duration_minutes": forms.NumberInput(attrs={**_INPUT, "min": "1"}),
            "fleet_op_require_attendance": forms.CheckboxInput(attrs={"class": "h-4 w-4"}),
            "insurance_fraction": forms.NumberInput(
                attrs={**_INPUT, "step": "0.01", "min": "0", "max": "1"}),
            "intro_text": forms.Textarea(attrs={**_INPUT, "rows": "3"}),
        }

    def clean_insurance_fraction(self) -> Decimal:
        frac = self.cleaned_data["insurance_fraction"]
        if frac < 0 or frac > 1:
            raise forms.ValidationError("Insurance fraction must be between 0 and 1.")
        return frac

    def clean_default_cap(self) -> Decimal:
        cap = self.cleaned_data["default_cap"]
        if cap < 0:
            raise forms.ValidationError("Cap can't be negative.")
        return cap

    def clean_fleet_op_default_duration_minutes(self) -> int:
        mins = self.cleaned_data["fleet_op_default_duration_minutes"]
        if mins < 1:
            raise forms.ValidationError("Default op length must be at least 1 minute.")
        return mins


class SrpRuleForm(forms.ModelForm):
    class Meta:
        model = SrpRule
        fields = ["doctrine", "basis", "max_payout", "active"]
        widgets = {
            "doctrine": forms.Select(attrs=_INPUT),
            "basis": forms.Select(attrs=_INPUT),
            "max_payout": forms.NumberInput(attrs=_ISK),
            "active": forms.CheckboxInput(attrs={"class": "h-4 w-4"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A blank doctrine = the catch-all rule for any doctrine-tagged loss.
        self.fields["doctrine"].queryset = Doctrine.objects.order_by("name")
        self.fields["doctrine"].required = False
        self.fields["doctrine"].empty_label = "Any doctrine (catch-all)"

    def clean_max_payout(self) -> Decimal:
        amount = self.cleaned_data["max_payout"]
        if amount < 0:
            raise forms.ValidationError("Amount can't be negative.")
        return amount
