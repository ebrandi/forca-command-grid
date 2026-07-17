"""Officer forms for the procurement surfaces (P4, WS7).

Business data only: a supplier's profile, its catalogue, standing agreements and
purchase orders. Every user-visible label is gettext-wrapped; officer-typed prose
(display_name/contact/notes/reason) is verbatim and never machine-translated. The
lifecycle transitions themselves live in ``services``/``contracts``/``payments``/
``receipts`` — these forms only capture the officer's intent.
"""
from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.market.models import MarketLocation

from .models import (
    PurchaseOrder,
    PurchaseOrderLine,
    Supplier,
    SupplierItem,
    SupplyAgreement,
    SupplyAgreementLine,
)

_INPUT = {"class": "input-field"}
_DATE = {**_INPUT, "type": "date"}


def _active_locations():
    return MarketLocation.objects.filter(active=True).order_by("name")


class SupplierForm(forms.ModelForm):
    """A supplier's profile — a pilot, corp or hub source. ``entity_id`` is the
    EVE character/corporation id (blank for an informal hub)."""

    class Meta:
        model = Supplier
        fields = [
            "kind", "entity_id", "display_name", "contact", "default_location",
            "lead_time_days", "weekly_capacity_units", "status", "notes",
        ]
        widgets = {
            "kind": forms.Select(attrs=_INPUT),
            "entity_id": forms.NumberInput(attrs={**_INPUT, "min": 1}),
            "display_name": forms.TextInput(attrs=_INPUT),
            "contact": forms.TextInput(attrs=_INPUT),
            "default_location": forms.Select(attrs=_INPUT),
            "lead_time_days": forms.NumberInput(attrs={**_INPUT, "min": 0, "max": 365}),
            "weekly_capacity_units": forms.NumberInput(attrs={**_INPUT, "min": 0}),
            "status": forms.Select(attrs=_INPUT),
            "notes": forms.Textarea(attrs={**_INPUT, "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["default_location"].queryset = _active_locations()
        self.fields["default_location"].required = False


class SupplierItemForm(forms.ModelForm):
    """One catalogue line: what the supplier sells/builds, at what MOQ and price."""

    class Meta:
        model = SupplierItem
        fields = [
            "type_id", "moq", "price_model", "fixed_price_isk", "premium_pct",
            "lead_time_days", "weekly_capacity_units", "active",
        ]
        widgets = {
            "type_id": forms.NumberInput(attrs={**_INPUT, "min": 1}),
            "moq": forms.NumberInput(attrs={**_INPUT, "min": 1}),
            "price_model": forms.Select(attrs=_INPUT),
            "fixed_price_isk": forms.NumberInput(attrs={**_INPUT, "step": "0.01", "min": "0"}),
            "premium_pct": forms.NumberInput(attrs={**_INPUT, "step": "0.0001"}),
            "lead_time_days": forms.NumberInput(attrs={**_INPUT, "min": 0, "max": 365}),
            "weekly_capacity_units": forms.NumberInput(attrs={**_INPUT, "min": 0}),
        }


class SupplyAgreementForm(forms.ModelForm):
    """A standing commitment header — its lines are added on the detail page while
    the agreement is still a draft."""

    class Meta:
        model = SupplyAgreement
        fields = [
            "supplier", "term_start", "term_end", "cadence", "location",
            "payment_terms", "collateral_isk", "notes",
        ]
        widgets = {
            "supplier": forms.Select(attrs=_INPUT),
            "term_start": forms.DateInput(attrs=_DATE),
            "term_end": forms.DateInput(attrs=_DATE),
            "cadence": forms.Select(attrs=_INPUT),
            "location": forms.Select(attrs=_INPUT),
            "payment_terms": forms.Select(attrs=_INPUT),
            "collateral_isk": forms.NumberInput(attrs={**_INPUT, "step": "0.01", "min": "0"}),
            "notes": forms.Textarea(attrs={**_INPUT, "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = Supplier.objects.exclude(
            status=Supplier.Status.RETIRED
        ).order_by("display_name", "pk")
        self.fields["location"].queryset = _active_locations()
        self.fields["location"].required = False


class SupplyAgreementLineForm(forms.ModelForm):
    """One concrete type on an agreement (a family is one line per type)."""

    class Meta:
        model = SupplyAgreementLine
        fields = [
            "type_id", "quantity_per_cycle", "min_qty", "max_qty",
            "price_model", "fixed_price_isk", "premium_pct",
        ]
        widgets = {
            "type_id": forms.NumberInput(attrs={**_INPUT, "min": 1}),
            "quantity_per_cycle": forms.NumberInput(attrs={**_INPUT, "min": 1}),
            "min_qty": forms.NumberInput(attrs={**_INPUT, "min": 0}),
            "max_qty": forms.NumberInput(attrs={**_INPUT, "min": 0}),
            "price_model": forms.Select(attrs=_INPUT),
            "fixed_price_isk": forms.NumberInput(attrs={**_INPUT, "step": "0.01", "min": "0"}),
            "premium_pct": forms.NumberInput(attrs={**_INPUT, "step": "0.0001"}),
        }

    def clean_quantity_per_cycle(self):
        value = self.cleaned_data.get("quantity_per_cycle")
        if value is not None and value < 1:
            raise forms.ValidationError(_("The per-cycle quantity must be at least 1."))
        return value


class PurchaseOrderForm(forms.ModelForm):
    """A purchase-order header — its lines are added on the detail page while the
    order is still a draft, then it is submitted for approval."""

    class Meta:
        model = PurchaseOrder
        fields = ["supplier", "agreement", "location", "delivery_mode", "notes"]
        widgets = {
            "supplier": forms.Select(attrs=_INPUT),
            "agreement": forms.Select(attrs=_INPUT),
            "location": forms.Select(attrs=_INPUT),
            "delivery_mode": forms.Select(attrs=_INPUT),
            "notes": forms.Textarea(attrs={**_INPUT, "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = Supplier.objects.exclude(
            status=Supplier.Status.RETIRED
        ).order_by("display_name", "pk")
        self.fields["agreement"].queryset = SupplyAgreement.objects.filter(
            status=SupplyAgreement.Status.ACTIVE
        ).order_by("-created_at")
        self.fields["agreement"].required = False
        self.fields["location"].queryset = _active_locations()
        self.fields["location"].required = False


class PurchaseOrderLineForm(forms.ModelForm):
    """One type on a purchase order; ``doctrine_fit`` set ⇒ receipts post to fit
    inventory, null ⇒ to the corp stockpile."""

    class Meta:
        model = PurchaseOrderLine
        fields = ["type_id", "doctrine_fit", "quantity_ordered"]
        widgets = {
            "type_id": forms.NumberInput(attrs={**_INPUT, "min": 1}),
            "doctrine_fit": forms.Select(attrs=_INPUT),
            "quantity_ordered": forms.NumberInput(attrs={**_INPUT, "min": 1}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["doctrine_fit"].required = False

    def clean_quantity_ordered(self):
        value = self.cleaned_data.get("quantity_ordered")
        if value is None or value < 1:
            raise forms.ValidationError(_("Order at least one unit."))
        return value
