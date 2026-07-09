"""Member-facing forms for logistics (hauling) and manual stocktaking."""
from __future__ import annotations

from django import forms

from apps.market.models import MarketLocation

from .models import HaulingTask, Stockpile


class HaulingTaskForm(forms.ModelForm):
    type_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput())

    class Meta:
        model = HaulingTask
        fields = ["type_id", "quantity", "source_location", "dest_location"]
        widgets = {
            "quantity": forms.NumberInput(attrs={"class": "input-field", "min": 1}),
            "source_location": forms.Select(attrs={"class": "input-field"}),
            "dest_location": forms.Select(attrs={"class": "input-field"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        locs = MarketLocation.objects.order_by("name")
        self.fields["source_location"].queryset = locs
        self.fields["dest_location"].queryset = locs
        self.fields["source_location"].empty_label = "— from —"
        self.fields["dest_location"].empty_label = "— to —"

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("source_location") and cleaned.get("source_location") == cleaned.get("dest_location"):
            raise forms.ValidationError("Source and destination must differ.")
        return cleaned


class StockEntryForm(forms.Form):
    """Manual stocktake: record current (and optional target) for an item."""

    stockpile = forms.ModelChoiceField(
        queryset=Stockpile.objects.none(),
        widget=forms.Select(attrs={"class": "input-field"}),
    )
    type_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput())
    quantity_current = forms.IntegerField(
        min_value=0, widget=forms.NumberInput(attrs={"class": "input-field", "min": 0})
    )
    quantity_target = forms.IntegerField(
        min_value=0, required=False, widget=forms.NumberInput(attrs={"class": "input-field", "min": 0})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["stockpile"].queryset = Stockpile.objects.order_by("name")


class StockpileForm(forms.ModelForm):
    class Meta:
        model = Stockpile
        fields = ["name", "kind", "location"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "input-field", "placeholder": "e.g. Staging hangar"}),
            "kind": forms.Select(attrs={"class": "input-field"}),
            "location": forms.Select(attrs={"class": "input-field"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = MarketLocation.objects.order_by("name")
        self.fields["location"].required = False
