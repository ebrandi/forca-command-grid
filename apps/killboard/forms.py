"""Forms for intel watchlists and battle-report generation."""
from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import Watchlist, WatchlistEntry


class WatchlistForm(forms.ModelForm):
    class Meta:
        model = Watchlist
        fields = ["name", "purpose"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "input-field", "placeholder": _("e.g. Local gate campers")}),
            "purpose": forms.TextInput(attrs={"class": "input-field", "placeholder": _("Why we watch them")}),
        }


class WatchlistEntryForm(forms.ModelForm):
    class Meta:
        model = WatchlistEntry
        fields = ["entity_type", "entity_id", "note"]
        widgets = {
            "entity_type": forms.Select(attrs={"class": "input-field"}),
            "entity_id": forms.NumberInput(attrs={"class": "input-field", "min": 1, "placeholder": _("EVE ID")}),
            "note": forms.TextInput(attrs={"class": "input-field", "placeholder": _("Optional note")}),
        }

    def clean_entity_id(self):
        eid = self.cleaned_data["entity_id"]
        if eid < 1:
            raise forms.ValidationError(_("Enter a valid EVE entity id."))
        return eid


class BattleReportForm(forms.Form):
    """Generate a battle report from killmails in a system + time window."""

    title = forms.CharField(
        max_length=200, required=False,
        widget=forms.TextInput(attrs={"class": "input-field", "placeholder": _("Optional title")}),
    )
    system_id = forms.IntegerField(
        widget=forms.HiddenInput(),
    )
    hours = forms.IntegerField(
        min_value=1, max_value=168, initial=24,
        widget=forms.NumberInput(attrs={"class": "input-field", "min": 1, "max": 168}),
    )
