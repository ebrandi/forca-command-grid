"""Forms for the /ops/admin/raffle/ console (contest, prizes, sources, grants, config)."""
from __future__ import annotations

import json
from datetime import timedelta

from django import forms
from django.utils import timezone
from django.utils.translation import gettext, gettext_lazy as _

from . import metrics
from .models import (
    RaffleConfig,
    RaffleContest,
    RafflePrize,
    RaffleTicketSourceConfig,
)

_INPUT = {"class": "input-field"}
_ISK = {**_INPUT, "step": "1", "min": "0"}
_CHECK = {"class": "h-4 w-4"}
_DT_FMT = "%Y-%m-%dT%H:%M"


def _dt_widget():
    return forms.DateTimeInput(attrs={**_INPUT, "type": "datetime-local"}, format=_DT_FMT)


class _JSONField(forms.CharField):
    """A textarea that round-trips a JSON object/list, with friendly errors."""

    widget = forms.Textarea(attrs={**_INPUT, "rows": "4", "class": "input-field font-mono text-xs"})

    def prepare_value(self, value):
        if isinstance(value, dict | list):
            return json.dumps(value, indent=2)
        return value

    def clean(self, value):
        value = (value or "").strip()
        if not value:
            return {}
        try:
            return json.loads(value)
        except json.JSONDecodeError as e:
            raise forms.ValidationError(gettext("Invalid JSON: %(error)s") % {"error": e}) from e


class RaffleContestForm(forms.ModelForm):
    min_activity_metric = forms.ChoiceField(
        choices=metrics.CHOICES, required=False, widget=forms.Select(attrs=_INPUT))
    prize_booster_metric = forms.ChoiceField(
        choices=metrics.CHOICES, required=False, widget=forms.Select(attrs=_INPUT))

    class Meta:
        model = RaffleContest
        fields = [
            "name", "description", "objective", "public_rules", "admin_notes",
            "start_at", "end_at", "draw_at",
            "require_enrolled", "require_valid_token", "include_alliance",
            "retroactive_enabled", "one_prize_per_pilot", "auto_draw",
            "leaderboard_visible", "leaderboard_size", "show_odds",
            "show_recent_events", "show_ineligible_to_pilots", "archive_public",
            "min_activity_metric", "min_activity_threshold",
            "prize_booster_metric", "prize_booster_goal", "prize_booster_percent",
            "booster_multiplier", "booster_start_at", "booster_end_at",
        ]
        widgets = {
            "name": forms.TextInput(attrs=_INPUT),
            "description": forms.Textarea(attrs={**_INPUT, "rows": "2"}),
            "objective": forms.TextInput(attrs=_INPUT),
            "public_rules": forms.Textarea(attrs={**_INPUT, "rows": "4"}),
            "admin_notes": forms.Textarea(attrs={**_INPUT, "rows": "2"}),
            "start_at": _dt_widget(), "end_at": _dt_widget(), "draw_at": _dt_widget(),
            "require_enrolled": forms.CheckboxInput(attrs=_CHECK),
            "require_valid_token": forms.CheckboxInput(attrs=_CHECK),
            "include_alliance": forms.CheckboxInput(attrs=_CHECK),
            "retroactive_enabled": forms.CheckboxInput(attrs=_CHECK),
            "one_prize_per_pilot": forms.CheckboxInput(attrs=_CHECK),
            "auto_draw": forms.CheckboxInput(attrs=_CHECK),
            "leaderboard_visible": forms.CheckboxInput(attrs=_CHECK),
            "leaderboard_size": forms.NumberInput(attrs={**_INPUT, "min": "1", "max": "200"}),
            "show_odds": forms.CheckboxInput(attrs=_CHECK),
            "show_recent_events": forms.CheckboxInput(attrs=_CHECK),
            "show_ineligible_to_pilots": forms.CheckboxInput(attrs=_CHECK),
            "archive_public": forms.CheckboxInput(attrs=_CHECK),
            "min_activity_threshold": forms.NumberInput(attrs={**_INPUT, "min": "0", "step": "1"}),
            "prize_booster_goal": forms.NumberInput(attrs={**_INPUT, "min": "0", "step": "1"}),
            "prize_booster_percent": forms.NumberInput(
                attrs={**_INPUT, "min": "0", "max": "1000", "step": "1"}),
            "booster_multiplier": forms.NumberInput(attrs={**_INPUT, "step": "0.25", "min": "1"}),
            "booster_start_at": _dt_widget(), "booster_end_at": _dt_widget(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in ("start_at", "end_at", "draw_at", "booster_start_at", "booster_end_at"):
            self.fields[f].input_formats = [_DT_FMT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"]
        self.fields["booster_start_at"].required = False
        self.fields["booster_end_at"].required = False
        # Numeric safeguard/booster fields are optional (blank ⇒ 0, i.e. "off").
        for f in ("min_activity_threshold", "prize_booster_goal", "prize_booster_percent"):
            self.fields[f].required = False

    def clean(self):
        cleaned = super().clean()
        # Blank numeric safeguard/booster inputs mean "off" — coerce to 0 so the
        # not-null model fields save cleanly.
        for f in ("min_activity_threshold", "prize_booster_goal", "prize_booster_percent"):
            if cleaned.get(f) in (None, ""):
                cleaned[f] = 0
        start, end, draw = cleaned.get("start_at"), cleaned.get("end_at"), cleaned.get("draw_at")
        # The start date can never be set in the past. Only enforced when the start
        # is actually being set/changed (so editing other fields on a contest whose
        # start was already fixed isn't blocked); a 1-minute grace absorbs the pick /
        # submit lag so "start now" still works.
        if start and "start_at" in self.changed_data and start < timezone.now() - timedelta(minutes=1):
            self.add_error("start_at", _("The start date can't be in the past."))
        if start and end and end <= start:
            self.add_error("end_at", _("End must be after the start."))
        if end and draw and draw < end:
            self.add_error("draw_at", _("The draw can't be before accrual ends."))
        bs, be = cleaned.get("booster_start_at"), cleaned.get("booster_end_at")
        if bool(bs) ^ bool(be):
            self.add_error("booster_end_at", _("Set both booster start and end, or neither."))
        if bs and be and be <= bs:
            self.add_error("booster_end_at", _("Booster end must be after its start."))
        # A minimum-activity metric needs a positive threshold to mean anything.
        if cleaned.get("min_activity_metric") and (cleaned.get("min_activity_threshold") or 0) <= 0:
            self.add_error("min_activity_threshold",
                           _("Set a positive threshold (or clear the metric)."))
        # A prize-value booster needs both a positive goal and a positive percent.
        if cleaned.get("prize_booster_metric"):
            if (cleaned.get("prize_booster_goal") or 0) <= 0:
                self.add_error("prize_booster_goal", _("Set a positive goal (or clear the metric)."))
            if (cleaned.get("prize_booster_percent") or 0) <= 0:
                self.add_error("prize_booster_percent", _("Set a positive boost percent."))
        return cleaned


class RafflePrizeForm(forms.ModelForm):
    class Meta:
        model = RafflePrize
        fields = ["rank", "name", "prize_type", "icon_type_id", "quantity",
                  "estimated_value", "description", "delivery_instructions", "internal_notes"]
        widgets = {
            "rank": forms.NumberInput(attrs={**_INPUT, "min": "1"}),
            "name": forms.TextInput(attrs=_INPUT),
            "prize_type": forms.Select(attrs=_INPUT),
            "icon_type_id": forms.NumberInput(attrs=_INPUT),
            "quantity": forms.NumberInput(attrs={**_INPUT, "min": "1"}),
            "estimated_value": forms.NumberInput(attrs=_ISK),
            "description": forms.Textarea(attrs={**_INPUT, "rows": "2"}),
            "delivery_instructions": forms.Textarea(attrs={**_INPUT, "rows": "2"}),
            "internal_notes": forms.Textarea(attrs={**_INPUT, "rows": "2"}),
        }


class RaffleSourceConfigForm(forms.ModelForm):
    config = _JSONField(required=False, help_text=_("Source rate/rules as JSON."))
    filters = _JSONField(required=False, help_text=_("Eligibility filters as JSON."))

    class Meta:
        model = RaffleTicketSourceConfig
        fields = ["enabled", "mode", "config", "filters", "min_threshold",
                  "max_per_event", "cap_scope", "cap_amount", "require_esi",
                  "retroactive", "visible_to_pilots", "show_calculation"]
        widgets = {
            "enabled": forms.CheckboxInput(attrs=_CHECK),
            "mode": forms.Select(attrs=_INPUT),
            "min_threshold": forms.NumberInput(attrs=_ISK),
            "max_per_event": forms.NumberInput(attrs={**_INPUT, "min": "0"}),
            "cap_scope": forms.Select(attrs=_INPUT),
            "cap_amount": forms.NumberInput(attrs={**_INPUT, "min": "0"}),
            "require_esi": forms.CheckboxInput(attrs=_CHECK),
            "retroactive": forms.CheckboxInput(attrs=_CHECK),
            "visible_to_pilots": forms.CheckboxInput(attrs=_CHECK),
            "show_calculation": forms.CheckboxInput(attrs=_CHECK),
        }


class RaffleManualGrantForm(forms.Form):
    character_id = forms.IntegerField(
        widget=forms.NumberInput(attrs={**_INPUT, "placeholder": _("EVE character id")}),
        help_text=_("The pilot's EVE character id (their main)."),
    )
    amount = forms.IntegerField(min_value=1, widget=forms.NumberInput(attrs={**_INPUT, "min": "1"}))
    reason = forms.CharField(widget=forms.TextInput(attrs=_INPUT))
    category = forms.CharField(required=False, widget=forms.TextInput(
        attrs={**_INPUT, "placeholder": _("e.g. Fleet command, Logistics, Newbro help")}))
    internal_notes = forms.CharField(required=False, widget=forms.Textarea(attrs={**_INPUT, "rows": "2"}))
    override = forms.BooleanField(
        required=False, widget=forms.CheckboxInput(attrs=_CHECK),
        help_text=_("Emergency override (Director only, must be enabled in config)."),
    )


class RaffleConfigForm(forms.ModelForm):
    class Meta:
        model = RaffleConfig
        fields = ["allow_manual_override", "intro_text",
                  "monthly_prize_budget", "budget_warn_pct"]
        widgets = {
            "allow_manual_override": forms.CheckboxInput(attrs=_CHECK),
            "intro_text": forms.Textarea(attrs={**_INPUT, "rows": "3"}),
            "monthly_prize_budget": forms.NumberInput(attrs={**_INPUT, "min": "0", "step": "1"}),
            "budget_warn_pct": forms.NumberInput(attrs={**_INPUT, "min": "1", "max": "100"}),
        }
