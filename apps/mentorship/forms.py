"""Forms: pilot registration + leadership config/CRUD.

Registration forms are plain ``forms.Form`` (they map to a services call, not a
single model, and normalise comma-separated languages/interests into JSON lists).
Config/CRUD use ``ModelForm`` (mirrors ``apps.srp.forms``). Task editing parses
``criteria``/``tags`` in the console view (like onboarding milestones) because the
criteria shape depends on the chosen validation type.
"""
from __future__ import annotations

from django import forms

from .models import (
    MenteeProfile,
    MentorshipBadge,
    MentorshipCohort,
    MentorshipProgram,
    MentorshipRewardRule,
    MentorshipTask,
    MentorshipTrack,
)

_INPUT = {"class": "input-field"}
_CHECK = {"class": "h-4 w-4"}
_AREA_CHOICES = MentorshipTrack.Category.choices


def _split(raw: str) -> list[str]:
    return [t.strip() for t in (raw or "").replace(";", ",").split(",") if t.strip()]


# ---------------------------------------------------------------------------
# Pilot registration
# ---------------------------------------------------------------------------
class MentorRegistrationForm(forms.Form):
    areas = forms.MultipleChoiceField(
        choices=_AREA_CHOICES, required=False, widget=forms.CheckboxSelectMultiple,
        label="Areas you can mentor",
    )
    timezone = forms.CharField(required=False, widget=forms.TextInput(attrs=_INPUT),
                               label="Time zone", help_text="e.g. EU, US East, AU.")
    play_windows = forms.CharField(required=False, widget=forms.TextInput(attrs=_INPUT),
                                   label="Usual play windows", help_text="e.g. weekday evenings 19:00–23:00 EVE.")
    languages = forms.CharField(required=False, widget=forms.TextInput(attrs=_INPUT),
                                help_text="Comma-separated, e.g. English, Portuguese.")
    comms = forms.CharField(required=False, widget=forms.TextInput(attrs=_INPUT),
                            label="Preferred comms", help_text="e.g. Discord voice, Mumble.")
    max_active_mentees = forms.IntegerField(required=False, min_value=0,
                                            widget=forms.NumberInput(attrs=_INPUT),
                                            help_text="0 = use the programme default.")
    open_to_adhoc = forms.BooleanField(required=False, initial=True,
                                       widget=forms.CheckboxInput(attrs=_CHECK),
                                       label="Open to ad-hoc questions")
    bio = forms.CharField(required=False, widget=forms.Textarea(attrs={**_INPUT, "rows": 3}),
                          label="Short mentor bio")
    restrictions = forms.CharField(required=False, widget=forms.TextInput(attrs=_INPUT),
                                   help_text='e.g. "PvP only", "EU evenings only".')

    def to_data(self) -> dict:
        c = self.cleaned_data
        return {
            "areas": c.get("areas") or [],
            "timezone": c.get("timezone", ""),
            "play_windows": c.get("play_windows", ""),
            "languages": _split(c.get("languages", "")),
            "comms": c.get("comms", ""),
            "max_active_mentees": c.get("max_active_mentees") or 0,
            "open_to_adhoc": bool(c.get("open_to_adhoc")),
            "bio": c.get("bio", ""),
            "restrictions": c.get("restrictions", ""),
        }

    @classmethod
    def from_profile(cls, profile):
        if profile is None:
            return cls()
        return cls(initial={
            "areas": profile.areas, "timezone": profile.timezone,
            "play_windows": profile.play_windows, "languages": ", ".join(profile.languages or []),
            "comms": profile.comms, "max_active_mentees": profile.max_active_mentees,
            "open_to_adhoc": profile.open_to_adhoc, "bio": profile.bio,
            "restrictions": profile.restrictions,
        })


class MenteeRegistrationForm(forms.Form):
    goals = forms.MultipleChoiceField(
        choices=_AREA_CHOICES, required=False, widget=forms.CheckboxSelectMultiple,
        label="What do you want to learn?",
    )
    experience = forms.ChoiceField(
        choices=MenteeProfile.Experience.choices, widget=forms.Select(attrs=_INPUT),
        label="Your experience level",
    )
    timezone = forms.CharField(required=False, widget=forms.TextInput(attrs=_INPUT), label="Time zone")
    play_windows = forms.CharField(required=False, widget=forms.TextInput(attrs=_INPUT),
                                   label="Usual play windows")
    languages = forms.CharField(required=False, widget=forms.TextInput(attrs=_INPUT),
                                help_text="Comma-separated.")
    interests = forms.CharField(required=False, widget=forms.TextInput(attrs=_INPUT),
                                help_text="Free-text interests, comma-separated.")
    ships_can_fly = forms.CharField(required=False, widget=forms.TextInput(attrs=_INPUT),
                                    label="Ships you can fly")
    needs_skill_help = forms.BooleanField(required=False, widget=forms.CheckboxInput(attrs=_CHECK),
                                          label="I'd like help with skill planning")
    needs_fitting_help = forms.BooleanField(required=False, widget=forms.CheckboxInput(attrs=_CHECK),
                                            label="I'd like help with fitting")
    voice_comfortable = forms.BooleanField(required=False, widget=forms.CheckboxInput(attrs=_CHECK),
                                           label="I'm comfortable on voice comms")
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={**_INPUT, "rows": 3}),
                            label="Anything to help matching?")

    def to_data(self) -> dict:
        c = self.cleaned_data
        return {
            "goals": c.get("goals") or [],
            "experience": c.get("experience"),
            "timezone": c.get("timezone", ""),
            "play_windows": c.get("play_windows", ""),
            "languages": _split(c.get("languages", "")),
            "interests": _split(c.get("interests", "")),
            "ships_can_fly": c.get("ships_can_fly", ""),
            "needs_skill_help": bool(c.get("needs_skill_help")),
            "needs_fitting_help": bool(c.get("needs_fitting_help")),
            "voice_comfortable": bool(c.get("voice_comfortable")),
            "notes": c.get("notes", ""),
        }

    @classmethod
    def from_profile(cls, profile):
        if profile is None:
            return cls()
        return cls(initial={
            "goals": profile.goals, "experience": profile.experience, "timezone": profile.timezone,
            "play_windows": profile.play_windows, "languages": ", ".join(profile.languages or []),
            "interests": ", ".join(profile.interests or []), "ships_can_fly": profile.ships_can_fly,
            "needs_skill_help": profile.needs_skill_help, "needs_fitting_help": profile.needs_fitting_help,
            "voice_comfortable": profile.voice_comfortable, "notes": profile.notes,
        })


# ---------------------------------------------------------------------------
# Leadership config / CRUD
# ---------------------------------------------------------------------------
class MentorshipProgramForm(forms.ModelForm):
    class Meta:
        model = MentorshipProgram
        fields = [
            "enabled", "intro_text",
            "mentor_min_character_age_days", "mentor_min_corp_tenure_days",
            "mentor_eligibility_logic", "mentee_max_corp_tenure_days", "enforce_mentee_eligibility",
            "mentor_requires_approval", "mentee_requires_approval",
            "max_active_mentees_per_mentor", "allow_mentor_initiated", "allow_mentee_initiated",
            "pairing_requires_approval", "pairing_ttl_days", "stale_pair_days",
            "rewards_enabled", "reward_mode", "esi_validation_required", "allow_unverified_rewards",
            "default_task_cooldown_hours", "mentee_reward_cap_isk", "mentor_reward_cap_isk",
            "mentor_directory_visible", "profile_visibility", "notify_discord", "active_cohort",
        ]
        widgets = {
            "intro_text": forms.Textarea(attrs={**_INPUT, "rows": 3}),
            "mentor_eligibility_logic": forms.Select(attrs=_INPUT),
            "reward_mode": forms.Select(attrs=_INPUT),
            "profile_visibility": forms.Select(attrs=_INPUT),
            "active_cohort": forms.Select(attrs=_INPUT),
            "mentor_min_character_age_days": forms.NumberInput(attrs=_INPUT),
            "mentor_min_corp_tenure_days": forms.NumberInput(attrs=_INPUT),
            "mentee_max_corp_tenure_days": forms.NumberInput(attrs=_INPUT),
            "max_active_mentees_per_mentor": forms.NumberInput(attrs=_INPUT),
            "pairing_ttl_days": forms.NumberInput(attrs=_INPUT),
            "stale_pair_days": forms.NumberInput(attrs=_INPUT),
            "default_task_cooldown_hours": forms.NumberInput(attrs=_INPUT),
            "mentee_reward_cap_isk": forms.NumberInput(attrs={**_INPUT, "step": "1", "min": "0"}),
            "mentor_reward_cap_isk": forms.NumberInput(attrs={**_INPUT, "step": "1", "min": "0"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["active_cohort"].queryset = MentorshipCohort.objects.order_by("-starts_on")
        self.fields["active_cohort"].required = False
        self.fields["active_cohort"].empty_label = "— none —"
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "h-4 w-4")


class CohortForm(forms.ModelForm):
    class Meta:
        model = MentorshipCohort
        fields = ["key", "name", "description", "starts_on", "ends_on", "is_active", "sort_order"]
        widgets = {
            "key": forms.TextInput(attrs=_INPUT), "name": forms.TextInput(attrs=_INPUT),
            "description": forms.Textarea(attrs={**_INPUT, "rows": 2}),
            "starts_on": forms.DateInput(attrs={**_INPUT, "type": "date"}),
            "ends_on": forms.DateInput(attrs={**_INPUT, "type": "date"}),
            "is_active": forms.CheckboxInput(attrs=_CHECK),
            "sort_order": forms.NumberInput(attrs=_INPUT),
        }


class TrackForm(forms.ModelForm):
    class Meta:
        model = MentorshipTrack
        fields = ["key", "title", "summary", "description", "category", "icon",
                  "is_core", "estimated_sessions", "sort_order", "active"]
        widgets = {
            "key": forms.TextInput(attrs=_INPUT), "title": forms.TextInput(attrs=_INPUT),
            "summary": forms.TextInput(attrs=_INPUT),
            "description": forms.Textarea(attrs={**_INPUT, "rows": 2}),
            "category": forms.Select(attrs=_INPUT), "icon": forms.TextInput(attrs=_INPUT),
            "is_core": forms.CheckboxInput(attrs=_CHECK),
            "estimated_sessions": forms.NumberInput(attrs=_INPUT),
            "sort_order": forms.NumberInput(attrs=_INPUT), "active": forms.CheckboxInput(attrs=_CHECK),
        }


class TaskForm(forms.ModelForm):
    """Scalar fields only — ``criteria`` and ``tags`` are parsed in the view."""

    class Meta:
        model = MentorshipTask
        fields = ["track", "title", "description", "difficulty", "estimated_minutes",
                  "participants", "mentor_instructions", "mentee_instructions",
                  "validation_method", "evidence_requirement", "evidence_kind",
                  "reward_eligible", "cooldown_hours", "repeatable", "max_repeats",
                  "mandatory", "visibility", "sort_order", "active"]
        widgets = {
            "track": forms.Select(attrs=_INPUT), "title": forms.TextInput(attrs=_INPUT),
            "description": forms.Textarea(attrs={**_INPUT, "rows": 2}),
            "difficulty": forms.Select(attrs=_INPUT),
            "estimated_minutes": forms.NumberInput(attrs=_INPUT),
            "participants": forms.Select(attrs=_INPUT),
            "mentor_instructions": forms.Textarea(attrs={**_INPUT, "rows": 2}),
            "mentee_instructions": forms.Textarea(attrs={**_INPUT, "rows": 2}),
            "validation_method": forms.Select(attrs=_INPUT),
            "evidence_requirement": forms.Select(attrs=_INPUT),
            "evidence_kind": forms.Select(attrs=_INPUT),
            "reward_eligible": forms.CheckboxInput(attrs=_CHECK),
            "cooldown_hours": forms.NumberInput(attrs=_INPUT),
            "repeatable": forms.CheckboxInput(attrs=_CHECK),
            "max_repeats": forms.NumberInput(attrs=_INPUT),
            "mandatory": forms.CheckboxInput(attrs=_CHECK),
            "visibility": forms.Select(attrs=_INPUT),
            "sort_order": forms.NumberInput(attrs=_INPUT),
            "active": forms.CheckboxInput(attrs=_CHECK),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["track"].queryset = MentorshipTrack.objects.order_by("sort_order", "title")


class RewardRuleForm(forms.ModelForm):
    class Meta:
        model = MentorshipRewardRule
        fields = ["key", "label", "description", "audience", "trigger", "trigger_ref",
                  "reward_type", "amount", "points", "badge", "title_text",
                  "cap_per_recipient", "cooldown_hours", "requires_leadership_approval",
                  "requires_verification", "cohort", "active", "sort_order"]
        widgets = {
            "key": forms.TextInput(attrs=_INPUT), "label": forms.TextInput(attrs=_INPUT),
            "description": forms.TextInput(attrs=_INPUT),
            "audience": forms.Select(attrs=_INPUT), "trigger": forms.Select(attrs=_INPUT),
            "trigger_ref": forms.TextInput(attrs=_INPUT), "reward_type": forms.Select(attrs=_INPUT),
            "amount": forms.NumberInput(attrs={**_INPUT, "step": "1", "min": "0"}),
            "points": forms.NumberInput(attrs=_INPUT), "badge": forms.Select(attrs=_INPUT),
            "title_text": forms.TextInput(attrs=_INPUT),
            "cap_per_recipient": forms.NumberInput(attrs={**_INPUT, "step": "1", "min": "0"}),
            "cooldown_hours": forms.NumberInput(attrs=_INPUT),
            "requires_leadership_approval": forms.CheckboxInput(attrs=_CHECK),
            "requires_verification": forms.CheckboxInput(attrs=_CHECK),
            "cohort": forms.Select(attrs=_INPUT), "active": forms.CheckboxInput(attrs=_CHECK),
            "sort_order": forms.NumberInput(attrs=_INPUT),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["badge"].queryset = MentorshipBadge.objects.order_by("sort_order")
        self.fields["badge"].required = False
        self.fields["badge"].empty_label = "— none —"
        self.fields["cohort"].queryset = MentorshipCohort.objects.order_by("-starts_on")
        self.fields["cohort"].required = False
        self.fields["cohort"].empty_label = "— any cohort —"


class BadgeForm(forms.ModelForm):
    class Meta:
        model = MentorshipBadge
        fields = ["key", "label", "description", "icon", "tier", "audience", "sort_order", "active"]
        widgets = {
            "key": forms.TextInput(attrs=_INPUT), "label": forms.TextInput(attrs=_INPUT),
            "description": forms.TextInput(attrs=_INPUT), "icon": forms.TextInput(attrs=_INPUT),
            "tier": forms.Select(attrs=_INPUT), "audience": forms.Select(attrs=_INPUT),
            "sort_order": forms.NumberInput(attrs=_INPUT), "active": forms.CheckboxInput(attrs=_CHECK),
        }
