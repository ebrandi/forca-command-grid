"""Forms for the freight calculator, contract creation, and rate management."""
from __future__ import annotations

from decimal import Decimal

from django import forms

from .models import RateCard, ShipClass

_INPUT = {"class": "input-field"}


class QuoteForm(forms.Form):
    """A freight quote request. Origin/destination come from the location picker
    (station / structure / system, each carrying a system id for routing); a
    manual jump count is the fallback when no route resolves."""

    ship_class = forms.ChoiceField(
        choices=ShipClass.choices, initial=ShipClass.FREIGHTER,
        widget=forms.Select(attrs=_INPUT),
    )
    # Picker-backed endpoints (the _location_picker component renders the inputs).
    origin_name = forms.CharField(max_length=200, required=False)
    origin_kind = forms.CharField(max_length=10, required=False)
    origin_id = forms.CharField(max_length=24, required=False)
    origin_system_id = forms.IntegerField(min_value=1, required=False)
    dest_name = forms.CharField(max_length=200, required=False)
    dest_kind = forms.CharField(max_length=10, required=False)
    dest_id = forms.CharField(max_length=24, required=False)
    dest_system_id = forms.IntegerField(min_value=1, required=False)

    jumps = forms.IntegerField(
        min_value=1, max_value=200, required=False,
        widget=forms.NumberInput(attrs={**_INPUT, "placeholder": "auto from route"}),
    )
    volume_m3 = forms.FloatField(
        min_value=1,
        widget=forms.NumberInput(attrs={**_INPUT, "placeholder": "m³", "step": "1"}),
    )
    collateral = forms.DecimalField(
        min_value=0, initial=0, max_digits=20, decimal_places=2,
        widget=forms.NumberInput(attrs={**_INPUT, "placeholder": "ISK", "step": "1000000"}),
    )
    rush = forms.BooleanField(required=False)

    def clean(self):
        cleaned = super().clean()
        has_route = (cleaned.get("origin_system_id") and cleaned.get("dest_system_id")) or (
            cleaned.get("origin_name") and cleaned.get("dest_name")
        )
        if not cleaned.get("jumps") and not has_route:
            raise forms.ValidationError("Pick an origin and destination, or enter a manual jump count.")
        return cleaned


class RateCardForm(forms.ModelForm):
    class Meta:
        model = RateCard
        fields = [
            "name", "audience", "discount", "min_reward",
            "dst_rate_per_warp", "dst_lowsec_rate_per_warp", "dst_max_m3", "dst_max_collateral",
            "freighter_rate_per_warp", "freighter_rate_per_warp_long", "freighter_long_threshold",
            "freighter_max_m3", "freighter_max_collateral",
            "jf_base", "jf_per_jump", "jf_assumed_jdc", "jf_max_m3", "jf_max_collateral",
            "rush_fee_hs", "rush_fee_jf", "contract_days",
        ]
        widgets = {
            f: forms.NumberInput(attrs=_INPUT)
            for f in fields if f not in ("name", "audience")
        }
        widgets["name"] = forms.TextInput(attrs=_INPUT)
        widgets["audience"] = forms.Select(attrs=_INPUT)

    # ISK/quantity fields that must never be negative.
    _NON_NEGATIVE = [
        "min_reward", "dst_rate_per_warp", "dst_lowsec_rate_per_warp", "dst_max_m3",
        "dst_max_collateral", "freighter_rate_per_warp", "freighter_rate_per_warp_long",
        "freighter_long_threshold", "freighter_max_m3", "freighter_max_collateral",
        "jf_base", "jf_per_jump", "jf_assumed_jdc", "jf_max_m3", "jf_max_collateral",
        "rush_fee_hs", "rush_fee_jf", "contract_days",
    ]

    def clean(self):
        cleaned = super().clean()
        # The discount is a price multiplier; 0 would make every quote free.
        discount = cleaned.get("discount")
        if discount is not None and not (Decimal("0") < discount <= Decimal("2")):
            self.add_error("discount", "Must be greater than 0 and at most 2.")
        for field in self._NON_NEGATIVE:
            value = cleaned.get(field)
            if value is not None and value < 0:
                self.add_error(field, "Can't be negative.")
        return cleaned
