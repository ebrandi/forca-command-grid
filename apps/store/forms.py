"""Forms for ordering and store configuration."""
from __future__ import annotations

from decimal import Decimal

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import StoreConfig

_INPUT = {"class": "input-field"}


class HullOrderForm(forms.Form):
    ship_type_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput())
    quantity = forms.IntegerField(
        min_value=1, max_value=100, initial=1,
        widget=forms.NumberInput(attrs={**_INPUT, "min": 1}),
    )
    location_name = forms.CharField(
        max_length=200, required=False,
        widget=forms.TextInput(attrs={**_INPUT, "placeholder": _("Delivery / staging system")}),
    )
    notes = forms.CharField(
        max_length=300, required=False,
        widget=forms.TextInput(attrs={**_INPUT, "placeholder": _("Anything the builder should know")}),
    )


class FitOrderForm(forms.Form):
    fit_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput())
    quantity = forms.IntegerField(
        min_value=1, max_value=100, initial=1,
        widget=forms.NumberInput(attrs={**_INPUT, "min": 1}),
    )
    location_name = forms.CharField(
        max_length=200, required=False,
        widget=forms.TextInput(attrs={**_INPUT, "placeholder": _("Delivery / staging system")}),
    )
    notes = forms.CharField(max_length=300, required=False, widget=forms.HiddenInput())


class ConfigForm(forms.ModelForm):
    class Meta:
        model = StoreConfig
        fields = ["name", "audience", "doctrine_markup", "hull_markup", "deposit_pct"]
        widgets = {
            "name": forms.TextInput(attrs=_INPUT),
            "audience": forms.Select(attrs=_INPUT),
            "doctrine_markup": forms.NumberInput(attrs={**_INPUT, "step": "0.01", "min": "1"}),
            "hull_markup": forms.NumberInput(attrs={**_INPUT, "step": "0.01", "min": "1"}),
            "deposit_pct": forms.NumberInput(attrs={**_INPUT, "step": "0.01", "min": "0", "max": "1"}),
        }

    def _check(self, field, low, high):
        value = self.cleaned_data.get(field)
        if value is not None and not (low <= value <= high):
            self.add_error(field, _("Must be between %(low)s and %(high)s.") % {"low": low, "high": high})

    def clean(self):
        cleaned = super().clean()
        # Server-side bounds (the widget min/max are advisory only): never sell
        # below Jita, never an absurd markup, and a deposit is a fraction of price.
        self._check("doctrine_markup", Decimal("1"), Decimal("10"))
        self._check("hull_markup", Decimal("1"), Decimal("10"))
        self._check("deposit_pct", Decimal("0"), Decimal("1"))
        return cleaned
