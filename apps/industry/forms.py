"""Member-facing forms for creating and editing industry projects."""
from __future__ import annotations

from django import forms

from .models import IndustryProject, IndustryProjectItem


class ProjectForm(forms.ModelForm):
    class Meta:
        model = IndustryProject
        fields = ["name", "objective_type", "description"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "input-field", "placeholder": "e.g. Build 5 Feroxes"}),
            "objective_type": forms.Select(attrs={"class": "input-field"}),
            "description": forms.Textarea(attrs={"class": "input-field", "rows": 2}),
        }


class ProjectItemForm(forms.ModelForm):
    """Item line. ``type_id`` arrives from the autocomplete hidden field."""

    type_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput())

    class Meta:
        model = IndustryProjectItem
        fields = ["type_id", "quantity", "build_or_buy", "strategy", "me"]
        widgets = {
            "quantity": forms.NumberInput(attrs={"class": "input-field", "min": 1}),
            "build_or_buy": forms.Select(attrs={"class": "input-field"}),
            "strategy": forms.Select(attrs={"class": "input-field"}),
            "me": forms.NumberInput(attrs={"class": "input-field", "min": 0, "max": 10}),
        }

    def clean_quantity(self):
        qty = self.cleaned_data["quantity"]
        if qty < 1:
            raise forms.ValidationError("Quantity must be at least 1.")
        return qty

    def clean_me(self):
        me = self.cleaned_data["me"]
        if not 0 <= me <= 10:
            raise forms.ValidationError("Material efficiency is between 0 and 10.")
        return me
