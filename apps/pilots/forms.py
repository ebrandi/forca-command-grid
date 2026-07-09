"""Leadership forms for the pilot engagement spine."""
from __future__ import annotations

from django import forms

from .models import ContributionWeights

_INPUT = {"class": "input-field"}
_NUM = {**_INPUT, "step": "1", "min": "0"}
_DEC = {**_INPUT, "step": "0.01", "min": "0"}
_CHECK = {"class": "h-4 w-4"}


class ContributionWeightsForm(forms.ModelForm):
    class Meta:
        model = ContributionWeights
        fields = [
            "enabled",
            "task_points", "fleet_points", "haul_points", "haul_requires_verification",
            "build_points_per_ship", "mining_points_per_mil", "srp_points_per_mil",
            "train_points_per_level",
            "doctrine_base", "doctrine_priority_coef", "doctrine_effort_per_mil_sp",
            "pvp_points_per_kill", "pvp_final_blow_bonus",
            "pve_points_per_mil", "pve_ref_types",
        ]
        widgets = {
            "enabled": forms.CheckboxInput(attrs=_CHECK),
            "haul_requires_verification": forms.CheckboxInput(attrs=_CHECK),
            "task_points": forms.NumberInput(attrs=_NUM),
            "fleet_points": forms.NumberInput(attrs=_NUM),
            "haul_points": forms.NumberInput(attrs=_NUM),
            "build_points_per_ship": forms.NumberInput(attrs=_NUM),
            "mining_points_per_mil": forms.NumberInput(attrs={**_DEC, "step": "0.001"}),
            "srp_points_per_mil": forms.NumberInput(attrs={**_DEC, "step": "0.001"}),
            "train_points_per_level": forms.NumberInput(attrs=_NUM),
            "doctrine_base": forms.NumberInput(attrs=_NUM),
            "doctrine_priority_coef": forms.NumberInput(attrs=_DEC),
            "doctrine_effort_per_mil_sp": forms.NumberInput(attrs=_DEC),
            "pvp_points_per_kill": forms.NumberInput(attrs=_NUM),
            "pvp_final_blow_bonus": forms.NumberInput(attrs=_NUM),
            "pve_points_per_mil": forms.NumberInput(attrs={**_DEC, "step": "0.001"}),
            "pve_ref_types": forms.TextInput(attrs=_INPUT),
        }
