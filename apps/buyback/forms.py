"""Forms for the buyback appraisal, offer submission, and config."""
from __future__ import annotations

from decimal import Decimal

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import BuybackConfig, GuaranteedBuybackConfig, SecBand

_INPUT = {"class": "input-field"}


class AppraisalForm(forms.Form):
    items = forms.CharField(
        max_length=50_000,
        widget=forms.Textarea(attrs={
            "class": "input-field font-mono text-xs", "rows": 10,
            "placeholder": "Paste items from EVE, one per line:\nTritanium  1000000\nFerox  3\nIschoten II x5",
        }),
    )
    sec_band = forms.ChoiceField(
        choices=SecBand.choices, initial=SecBand.HIGHSEC,
        widget=forms.Select(attrs=_INPUT),
    )
    location_name = forms.CharField(
        max_length=200, required=False,
        widget=forms.TextInput(attrs={**_INPUT, "placeholder": _("Where the items are (e.g. station / system)")}),
    )
    notes = forms.CharField(
        max_length=300, required=False,
        widget=forms.TextInput(attrs={**_INPUT, "placeholder": _("Anything the buyer should know")}),
    )
    # 4.9: value ore/ice by refined mineral output. Ignored unless the config enables ore mode.
    ore = forms.BooleanField(required=False)


class ConfigForm(forms.ModelForm):
    class Meta:
        model = BuybackConfig
        fields = ["name", "audience", "highsec_pct", "lowsec_pct", "nullsec_pct",
                  "ore_mode_enabled", "reprocessing_pct"]
        widgets = {
            "name": forms.TextInput(attrs=_INPUT),
            "audience": forms.Select(attrs=_INPUT),
            "highsec_pct": forms.NumberInput(attrs={**_INPUT, "step": "0.01", "min": "0", "max": "1"}),
            "lowsec_pct": forms.NumberInput(attrs={**_INPUT, "step": "0.01", "min": "0", "max": "1"}),
            "nullsec_pct": forms.NumberInput(attrs={**_INPUT, "step": "0.01", "min": "0", "max": "1"}),
            "reprocessing_pct": forms.NumberInput(attrs={**_INPUT, "step": "0.001", "min": "0", "max": "1"}),
        }

    def clean(self):
        cleaned = super().clean()
        # Server-side bounds (widget min/max are advisory): a buyback pays a
        # fraction of Jita sell — never negative, never more than 100%. Same for the
        # reprocessing yield (a fraction of the mineral output).
        for field in ("highsec_pct", "lowsec_pct", "nullsec_pct", "reprocessing_pct"):
            value = cleaned.get(field)
            if value is not None and not (Decimal("0") <= value <= Decimal("1")):
                self.add_error(field, _("Must be between 0 and 1."))
        return cleaned


class GuaranteedConfigForm(forms.ModelForm):
    """Leadership arming + safety rails for the corp-funded guaranteed buyback (4.20)."""

    class Meta:
        model = GuaranteedBuybackConfig
        fields = ["enabled", "audience", "per_lot_cap", "daily_budget",
                  "require_esi_reconcile", "intro_text"]
        widgets = {
            "enabled": forms.CheckboxInput(attrs={"class": "h-4 w-4"}),
            "audience": forms.Select(attrs=_INPUT),
            "per_lot_cap": forms.NumberInput(attrs={**_INPUT, "step": "1000000", "min": "0"}),
            "daily_budget": forms.NumberInput(attrs={**_INPUT, "step": "1000000", "min": "0"}),
            "require_esi_reconcile": forms.CheckboxInput(attrs={"class": "h-4 w-4"}),
            "intro_text": forms.Textarea(attrs={**_INPUT, "rows": 2}),
        }

    def clean(self):
        cleaned = super().clean()
        # Caps must be non-negative; a negative budget would make every approval fail
        # confusingly (or, if mis-signed elsewhere, pass wrongly).
        for field in ("per_lot_cap", "daily_budget"):
            value = cleaned.get(field)
            if value is not None and value < 0:
                self.add_error(field, _("Must be zero or more."))
        return cleaned
