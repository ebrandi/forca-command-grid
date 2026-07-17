"""Forms for ordering and store configuration."""
from __future__ import annotations

from decimal import Decimal

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.market.models import MarketLocation

from .models import (
    FULFILMENT_STAMP_CHOICES,
    DemandConfig,
    FitOffer,
    MarginConfig,
    ShipyardPolicy,
    StoreConfig,
)

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
    """A doctrine-fit order request. Only the buyer's intent crosses the wire —
    price, availability, split, location and lead time are all re-derived
    server-side by ``place_fit_order`` (never trusted from the form)."""

    fit_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput())
    quantity = forms.IntegerField(
        min_value=1, max_value=1000, initial=1,
        widget=forms.NumberInput(attrs={**_INPUT, "min": 1}),
    )
    notes = forms.CharField(max_length=300, required=False, widget=forms.HiddenInput())
    # Set by the confirm page after the split/ETA was disclosed.
    acknowledge_backorder = forms.BooleanField(required=False)
    # Confirm-page choice when partial fulfilment is disabled: take everything as
    # a backorder instead of reducing the quantity to what is in stock.
    force_backorder = forms.BooleanField(required=False, widget=forms.HiddenInput())


class ShipyardPolicyForm(forms.ModelForm):
    class Meta:
        model = ShipyardPolicy
        fields = [
            "backorders_enabled", "default_lead_days", "allow_partial_fulfilment",
            "reservation_expiry_days", "default_location", "max_order_quantity",
            "limited_stock_threshold", "show_unavailable", "available_only_default",
            "waitlist_enabled", "auto_allocate_receipts",
        ]
        widgets = {
            "default_lead_days": forms.NumberInput(attrs={**_INPUT, "min": 0, "max": 365}),
            "reservation_expiry_days": forms.NumberInput(attrs={**_INPUT, "min": 0, "max": 90}),
            "default_location": forms.Select(attrs=_INPUT),
            "max_order_quantity": forms.NumberInput(attrs={**_INPUT, "min": 1, "max": 1000}),
            "limited_stock_threshold": forms.NumberInput(attrs={**_INPUT, "min": 0, "max": 100}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["default_location"].queryset = MarketLocation.objects.filter(
            active=True
        ).order_by("name")

    def _bound(self, name, low, high):
        value = self.cleaned_data.get(name)
        if value is not None and not (low <= value <= high):
            self.add_error(name, _("Must be between %(low)s and %(high)s.") % {
                "low": low, "high": high,
            })

    def clean(self):
        cleaned = super().clean()
        # Server-side bounds — the widget min/max are advisory only.
        self._bound("default_lead_days", 0, 365)
        self._bound("reservation_expiry_days", 0, 90)
        self._bound("max_order_quantity", 1, 1000)
        self._bound("limited_stock_threshold", 0, 100)
        return cleaned


class FitOfferForm(forms.ModelForm):
    """Per-fit overrides. Empty numeric/location fields inherit the corp policy."""

    class Meta:
        model = FitOffer
        fields = [
            "is_offered", "backorders_allowed", "lead_days", "delivery_location",
            "max_backorder_quantity", "max_per_order", "safety_stock",
            "reorder_point", "target_stock", "preferred_fulfilment",
            "buyer_notes", "internal_notes", "priority",
        ]
        widgets = {
            "backorders_allowed": forms.NullBooleanSelect(attrs=_INPUT),
            "lead_days": forms.NumberInput(attrs={**_INPUT, "min": 0, "max": 365}),
            "delivery_location": forms.Select(attrs=_INPUT),
            "max_backorder_quantity": forms.NumberInput(attrs={**_INPUT, "min": 0}),
            "max_per_order": forms.NumberInput(attrs={**_INPUT, "min": 1, "max": 1000}),
            "safety_stock": forms.NumberInput(attrs={**_INPUT, "min": 0}),
            "reorder_point": forms.NumberInput(attrs={**_INPUT, "min": 0}),
            "target_stock": forms.NumberInput(attrs={**_INPUT, "min": 0}),
            "preferred_fulfilment": forms.Select(attrs=_INPUT),
            "buyer_notes": forms.TextInput(attrs=_INPUT),
            "internal_notes": forms.TextInput(attrs=_INPUT),
            "priority": forms.NumberInput(attrs={**_INPUT, "min": -100, "max": 100}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["delivery_location"].queryset = MarketLocation.objects.filter(
            active=True
        ).order_by("name")

    def clean(self):
        cleaned = super().clean()
        for name, low, high in [
            ("lead_days", 0, 365), ("max_per_order", 1, 1000),
            ("safety_stock", 0, 100000), ("max_backorder_quantity", 0, 100000),
            ("reorder_point", 0, 100000), ("target_stock", 0, 100000),
            ("priority", -100, 100),
        ]:
            value = cleaned.get(name)
            if value is not None and not (low <= value <= high):
                self.add_error(name, _("Must be between %(low)s and %(high)s.") % {
                    "low": low, "high": high,
                })
        return cleaned


class StockReceiptForm(forms.Form):
    """Officer receipt of newly assembled complete ships at a location."""

    location = forms.ModelChoiceField(
        queryset=MarketLocation.objects.none(), widget=forms.Select(attrs=_INPUT),
    )
    quantity = forms.IntegerField(
        min_value=1, max_value=100000, widget=forms.NumberInput(attrs={**_INPUT, "min": 1}),
    )
    reason = forms.CharField(
        max_length=300, required=False,
        widget=forms.TextInput(attrs={**_INPUT, "placeholder": _("e.g. built from restock batch #12")}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = MarketLocation.objects.filter(
            active=True
        ).order_by("name")


class StockAdjustForm(forms.Form):
    """Officer stocktake correction — the reason is mandatory and audited."""

    corrected_balance = forms.IntegerField(
        min_value=0, max_value=100000, widget=forms.NumberInput(attrs={**_INPUT, "min": 0}),
    )
    reason = forms.CharField(
        max_length=300,
        widget=forms.TextInput(attrs={**_INPUT, "placeholder": _("Why the balance is being corrected")}),
    )


class AdvanceOrderForm(forms.Form):
    """Optional evidence captured when advancing an order (cost & profitability phase).

    ``contract_id`` is offered on the READY step (the fulfilling in-game contract id);
    ``fulfilment_method`` on the DELIVERED step (which lane actually covered it). Both are
    optional — skipping them is legal and nothing blocks the advance. The method select is
    only honoured on partial/zero consumption; a fully-consumed reservation auto-stamps
    ``stock`` regardless."""

    contract_id = forms.IntegerField(
        required=False, min_value=1, max_value=9_223_372_036_854_775_807,
        widget=forms.NumberInput(attrs={
            **_INPUT, "min": 1, "placeholder": _("In-game contract id (optional)"),
        }),
    )
    fulfilment_method = forms.ChoiceField(
        required=False, choices=[("", "—")] + list(FULFILMENT_STAMP_CHOICES),
        widget=forms.Select(attrs=_INPUT),
    )


class OrderEtaForm(forms.Form):
    """Claimer/officer revision of the living delivery estimate."""

    current_eta = forms.DateField(widget=forms.DateInput(attrs={**_INPUT, "type": "date"}))
    delay_reason = forms.CharField(
        max_length=300, required=False,
        widget=forms.TextInput(attrs={**_INPUT, "placeholder": _("What changed (shown to the buyer)")}),
    )


class ConfigForm(forms.ModelForm):
    class Meta:
        model = StoreConfig
        fields = ["name", "audience", "doctrine_markup", "hull_markup",
                  "capital_markup", "supercap_markup", "deposit_pct"]
        widgets = {
            "name": forms.TextInput(attrs=_INPUT),
            "audience": forms.Select(attrs=_INPUT),
            "doctrine_markup": forms.NumberInput(attrs={**_INPUT, "step": "0.01", "min": "1"}),
            "hull_markup": forms.NumberInput(attrs={**_INPUT, "step": "0.01", "min": "1"}),
            "capital_markup": forms.NumberInput(attrs={**_INPUT, "step": "0.01", "min": "1"}),
            "supercap_markup": forms.NumberInput(attrs={**_INPUT, "step": "0.01", "min": "1"}),
            "deposit_pct": forms.NumberInput(attrs={**_INPUT, "step": "0.01", "min": "0", "max": "1"}),
        }

    def _check(self, field, low, high):
        value = self.cleaned_data.get(field)
        if value is not None and not (low <= value <= high):
            self.add_error(field, _("Must be between %(low)s and %(high)s.") % {"low": low, "high": high})

    def clean(self):
        cleaned = super().clean()
        # Server-side bounds (the widget min/max are advisory only): never sell
        # below the price basis (Jita sell, or build cost for capital-class hulls),
        # never an absurd markup, and a deposit is a fraction of price.
        self._check("doctrine_markup", Decimal("1"), Decimal("10"))
        self._check("hull_markup", Decimal("1"), Decimal("10"))
        self._check("capital_markup", Decimal("1"), Decimal("10"))
        self._check("supercap_markup", Decimal("1"), Decimal("10"))
        self._check("deposit_pct", Decimal("0"), Decimal("1"))
        return cleaned


class DemandLineForm(forms.Form):
    """Officer-entered manual demand line ("40 × Ferox by the 28th") (P2)."""

    quantity = forms.IntegerField(min_value=1, max_value=100_000)
    needed_by = forms.DateField(required=False)
    note = forms.CharField(required=False, max_length=200)
    campaign_id = forms.IntegerField(required=False, min_value=1)

    def clean_needed_by(self):
        from django.utils import timezone

        value = self.cleaned_data.get("needed_by")
        if value and value < timezone.localdate():
            raise forms.ValidationError(_("The date must be today or later."))
        return value


class MarginConfigForm(forms.ModelForm):
    """Leadership margin & drift thresholds — the Director margin console."""

    class Meta:
        model = MarginConfig
        fields = [
            "drift_check_enabled", "drift_threshold_pct", "drift_min_isk",
            "settlement_reconcile_enabled", "margin_window_days", "margin_alert_floor_pct",
        ]
        widgets = {
            "drift_threshold_pct": forms.NumberInput(attrs={**_INPUT, "step": "0.001", "min": "0", "max": "5"}),
            "drift_min_isk": forms.NumberInput(attrs={**_INPUT, "step": "1", "min": "0"}),
            "margin_window_days": forms.NumberInput(attrs={**_INPUT, "min": "1", "max": "365"}),
            "margin_alert_floor_pct": forms.NumberInput(attrs={**_INPUT, "step": "0.001", "min": "0", "max": "1"}),
        }

    def _bound(self, name, low, high):
        value = self.cleaned_data.get(name)
        if value is not None and not (low <= value <= high):
            self.add_error(name, _("Must be between %(low)s and %(high)s.") % {
                "low": low, "high": high,
            })

    def clean(self):
        cleaned = super().clean()
        # Server-side bounds — the widget min/max are advisory only.
        self._bound("drift_threshold_pct", Decimal("0"), Decimal("5"))
        self._bound("drift_min_isk", Decimal("0"), Decimal("1000000000000"))
        self._bound("margin_window_days", 1, 365)
        self._bound("margin_alert_floor_pct", Decimal("0"), Decimal("1"))
        return cleaned


class DemandConfigForm(forms.ModelForm):
    """Leadership demand-planning knobs (P2) — served by the native console page
    (the stock Django admin is disabled on the servers)."""

    class Meta:
        model = DemandConfig
        fields = [
            "history_weeks", "horizon_days", "service_level", "op_attrition_pct",
            "slow_mover_days", "include_untagged_losses", "include_recurring_ops",
            "use_suggested_reorder_alerts",
        ]
        widgets = {
            "history_weeks": forms.NumberInput(attrs={**_INPUT, "min": 4, "max": 52}),
            "horizon_days": forms.NumberInput(attrs={**_INPUT, "min": 7, "max": 120}),
            "service_level": forms.Select(attrs=_INPUT),
            "op_attrition_pct": forms.NumberInput(attrs={**_INPUT, "min": 0, "max": 100}),
            "slow_mover_days": forms.NumberInput(attrs={**_INPUT, "min": 7, "max": 365}),
        }

    def _bound(self, name, low, high):
        value = self.cleaned_data.get(name)
        if value is not None and not (low <= value <= high):
            self.add_error(name, _("Must be between %(low)s and %(high)s.") % {
                "low": low, "high": high,
            })

    def clean(self):
        cleaned = super().clean()
        # Server-side bounds — the widget min/max are advisory only.
        self._bound("history_weeks", 4, 52)
        self._bound("horizon_days", 7, 120)
        self._bound("op_attrition_pct", 0, 100)
        self._bound("slow_mover_days", 7, 365)
        return cleaned
