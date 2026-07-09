"""Forms for the PI planner. Every field carries help text — the pilot should never
wonder "what do I put here?". Planet rows are parsed separately (see views._parse_planets)."""
from __future__ import annotations

from django import forms

from .constants import TRADE_HUBS, hub_label
from .models import PiMaterial, PiPlan, PiVisibility, PlanetaryConfig

_INPUT = {"class": "input-field"}
_PCT = {**_INPUT, "step": "0.1", "min": "0", "max": "100"}
_NUM = {**_INPUT, "min": "0"}


class PiPlanForm(forms.ModelForm):
    """The guided plan form — used by both the create wizard and the editor."""

    character = forms.ChoiceField(
        required=False, widget=forms.Select(attrs=_INPUT),
        help_text="Which of your pilots will run this colony network. Used to label the "
                  "plan and to match imported colonies — nothing is changed in-game.")
    market_region_id = forms.TypedChoiceField(
        coerce=int, choices=[(h["region_id"], hub_label(h["region_id"])) for h in TRADE_HUBS],
        widget=forms.Select(attrs=_INPUT), label="Pricing hub",
        help_text="Where you'll sell. Prices and profit are estimated against this hub. "
                  "Jita is the most liquid and the safest default.")

    class Meta:
        model = PiPlan
        fields = [
            "name", "goal", "character", "system_name", "planet_count", "risk",
            "market_region_id", "customs_export_tax", "customs_import_tax", "sales_tax",
            "broker_fee", "hauling_cost_per_m3", "corp_buyback_rate", "extraction_rate_per_hour",
            "effort", "export_strategy", "visibility", "notes",
        ]
        widgets = {
            "name": forms.TextInput(attrs={**_INPUT, "placeholder": "e.g. Jita P2 Coolant run"}),
            "goal": forms.Select(attrs=_INPUT),
            "system_name": forms.TextInput(attrs={**_INPUT, "placeholder": "e.g. Amamake (optional)"}),
            "planet_count": forms.NumberInput(attrs={**_NUM, "max": "6"}),
            "risk": forms.Select(attrs=_INPUT),
            "customs_export_tax": forms.NumberInput(attrs=_PCT),
            "customs_import_tax": forms.NumberInput(attrs=_PCT),
            "sales_tax": forms.NumberInput(attrs=_PCT),
            "broker_fee": forms.NumberInput(attrs=_PCT),
            "hauling_cost_per_m3": forms.NumberInput(attrs=_NUM),
            "corp_buyback_rate": forms.NumberInput(attrs=_PCT),
            "extraction_rate_per_hour": forms.NumberInput(attrs=_NUM),
            "effort": forms.Select(attrs=_INPUT),
            "export_strategy": forms.Select(attrs=_INPUT),
            "visibility": forms.Select(attrs=_INPUT),
            "notes": forms.Textarea(attrs={**_INPUT, "rows": 3,
                     "placeholder": "Anything you want to remember about this setup."}),
        }
        help_texts = {
            "name": "A short name you'll recognise in your plan list.",
            "goal": "What you want out of PI. This tunes the recommendations and the guidance.",
            "system_name": "The system (or staging) you'll run this from. Optional, for your notes.",
            "planet_count": "How many planets you'll dedicate (1–6). Alpha clones are limited; "
                            "Omega pilots can run more with the right skills.",
            "risk": "Where you'll set up. Nullsec/wormhole planets yield more but cost you "
                    "hauling and danger — this is context for the guidance, not a hard rule.",
            "customs_export_tax": "Customs office (POCO) tax on export, as a %. In hostile "
                                  "space this can be high — check the POCO before you commit.",
            "customs_import_tax": "POCO tax on importing materials to a factory planet, as a %.",
            "sales_tax": "Market sales tax when you sell (%). Scales down with Accounting.",
            "broker_fee": "Broker fee on sell orders (%). 0 if you only sell to buy orders.",
            "hauling_cost_per_m3": "ISK per m³ to move goods to the hub. 0 if you haul yourself. "
                                   "Applied only when the export strategy is 'haul to hub'.",
            "corp_buyback_rate": "If you use corp buyback, the % of Jita sell it pays.",
            "extraction_rate_per_hour": "Planning assumption: P0 units/hour on one extraction "
                                        "planet. Tune it to your real colonies for accuracy.",
            "effort": "How often you'll reset extractors and haul. Low effort favours simple, "
                      "long-cycle setups.",
            "export_strategy": "What you'll do with the output — it changes which fees apply.",
            "visibility": "Who can see this plan. Private is only you.",
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [("", "— select a pilot —")]
        if user is not None:
            for c in user.characters.all().order_by("-is_main", "name"):
                choices.append((str(c.character_id), c.name))
        self.fields["character"].choices = choices
        if self.instance and self.instance.character_id:
            self.fields["character"].initial = str(self.instance.character_id)
        self._user = user

    def _clean_pct(self, field):
        value = self.cleaned_data.get(field)
        if value is not None and not (0 <= value <= 100):
            raise forms.ValidationError("Enter a percentage between 0 and 100.")
        return value

    def clean_customs_export_tax(self):
        return self._clean_pct("customs_export_tax")

    def clean_customs_import_tax(self):
        return self._clean_pct("customs_import_tax")

    def clean_sales_tax(self):
        return self._clean_pct("sales_tax")

    def clean_broker_fee(self):
        return self._clean_pct("broker_fee")

    def clean_corp_buyback_rate(self):
        return self._clean_pct("corp_buyback_rate")

    def clean_planet_count(self):
        n = self.cleaned_data.get("planet_count") or 0
        if not (1 <= n <= 6):
            raise forms.ValidationError("A pilot can run between 1 and 6 planets.")
        return n

    def save(self, commit=True):
        plan = super().save(commit=False)
        char_id = self.cleaned_data.get("character")
        if char_id and self._user is not None:
            char = self._user.characters.filter(character_id=char_id).first()
            if char:
                plan.character_id = char.character_id
                plan.character_name = char.name
        plan.market_region_name = hub_label(plan.market_region_id)
        if commit:
            plan.save()
        return plan


class PlanetaryConfigForm(forms.ModelForm):
    """Leadership defaults for the PI planner (Director-gated)."""

    default_market_region_id = forms.TypedChoiceField(
        coerce=int, choices=[(h["region_id"], hub_label(h["region_id"])) for h in TRADE_HUBS],
        widget=forms.Select(attrs=_INPUT), label="Default pricing hub")
    recommended_products_text = forms.CharField(
        required=False, widget=forms.Textarea(attrs={**_INPUT, "rows": 2}),
        label="Corp priority products",
        help_text="Comma-separated PI product names the corp wants pilots to make "
                  "(e.g. Coolant, Mechanical Parts, Robotics). They get a badge in the "
                  "recommendations.")

    class Meta:
        model = PlanetaryConfig
        fields = [
            "enabled", "name", "default_market_region_id", "default_extraction_rate_per_hour",
            "default_customs_export_tax", "default_customs_import_tax", "default_sales_tax",
            "default_broker_fee", "default_hauling_cost_per_m3", "corp_buyback_rate",
            "recommended_regions", "priority_note", "default_visibility",
        ]
        widgets = {
            "name": forms.TextInput(attrs=_INPUT),
            "enabled": forms.CheckboxInput(attrs={"class": "h-4 w-4"}),
            "default_extraction_rate_per_hour": forms.NumberInput(attrs=_NUM),
            "default_customs_export_tax": forms.NumberInput(attrs=_PCT),
            "default_customs_import_tax": forms.NumberInput(attrs=_PCT),
            "default_sales_tax": forms.NumberInput(attrs=_PCT),
            "default_broker_fee": forms.NumberInput(attrs=_PCT),
            "default_hauling_cost_per_m3": forms.NumberInput(attrs=_NUM),
            "corp_buyback_rate": forms.NumberInput(attrs=_PCT),
            "recommended_regions": forms.TextInput(attrs={**_INPUT,
                "placeholder": "e.g. lowsec staging, home constellation"}),
            "priority_note": forms.Textarea(attrs={**_INPUT, "rows": 2}),
            "default_visibility": forms.Select(attrs=_INPUT, choices=PiVisibility.choices),
        }
        help_texts = {
            "enabled": "Master switch. Off hides the whole planner from pilots.",
            "name": "A label for this config (only you see it).",
            "default_extraction_rate_per_hour": "Starting extraction assumption for new plans.",
            "corp_buyback_rate": "% of Jita sell the corp buyback pays, for plans that use it.",
            "recommended_regions": "Free text: where the corp suggests running PI.",
            "priority_note": "A short note shown to pilots about what the corp needs.",
            "default_visibility": "Default sharing for newly-created plans.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.recommended_products:
            names = list(PiMaterial.objects.filter(
                type_id__in=self.instance.recommended_products
            ).values_list("name", flat=True))
            self.fields["recommended_products_text"].initial = ", ".join(names)

    def save(self, commit=True):
        config = super().save(commit=False)
        raw = self.cleaned_data.get("recommended_products_text", "")
        names = [n.strip() for n in raw.split(",") if n.strip()]
        ids = []
        for name in names:
            mat = PiMaterial.objects.filter(name__iexact=name).first()
            if mat:
                ids.append(mat.type_id)
        config.recommended_products = ids
        if commit:
            config.save()
        return config
