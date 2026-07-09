"""Forms for market locations (officer-managed)."""
from __future__ import annotations

from django import forms

from .models import MarketLocation


class MarketLocationForm(forms.ModelForm):
    class Meta:
        model = MarketLocation
        fields = ["name", "location_type", "region_id", "system_id", "is_staging", "is_price_reference"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "input-field", "placeholder": "e.g. Amarr VIII (Oris)"}),
            "location_type": forms.Select(attrs={"class": "input-field"}),
            "region_id": forms.NumberInput(attrs={"class": "input-field", "placeholder": "Region ID"}),
            "system_id": forms.NumberInput(attrs={"class": "input-field", "placeholder": "System ID"}),
        }
