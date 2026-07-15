"""New Player Onboarding: milestones, progress, glossary."""
from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.sso.models import EveCharacter

from . import milestones_i18n


class OnboardingMilestone(models.Model):
    class Category(models.TextChoices):
        ACCOUNT = "account", _("Account")
        SKILLS = "skills", _("Skills")
        DOCTRINE = "doctrine", _("Doctrine")
        ACTIVITY = "activity", _("Activity")

    key = models.SlugField(max_length=64, unique=True)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=12, choices=Category.choices, default=Category.ACCOUNT)
    criteria = models.JSONField(default=dict, blank=True)
    # Where "do it" happens — an internal path (/auth/eve/scopes/) or an external
    # invite (Discord/Mumble). Optional; rendered as the milestone's action link.
    url = models.CharField(max_length=300, blank=True, default="")
    sort_order = models.IntegerField(default=0)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order"]

    def __str__(self) -> str:
        return self.title

    @property
    def title_i18n(self) -> str:
        return milestones_i18n.milestone_title_for(self.key, self.title)

    @property
    def description_i18n(self) -> str:
        return milestones_i18n.milestone_description_for(self.key, self.description)


class OnboardingProgress(models.Model):
    class Status(models.TextChoices):
        TODO = "todo", _("To do")
        IN_PROGRESS = "in_progress", _("In progress")
        DONE = "done", _("Done")

    character = models.ForeignKey(
        EveCharacter, on_delete=models.CASCADE, related_name="onboarding_progress"
    )
    milestone = models.ForeignKey(
        OnboardingMilestone, on_delete=models.CASCADE, related_name="progress"
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.TODO)
    completed_at = models.DateTimeField(null=True, blank=True)
    auto_detected = models.BooleanField(default=False)

    class Meta:
        unique_together = ("character", "milestone")


class GlossaryTerm(models.Model):
    term = models.CharField(max_length=100, unique=True)
    definition = models.TextField()
    links = models.JSONField(default=list, blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ["term"]

    def __str__(self) -> str:
        return self.term

    @property
    def definition_i18n(self) -> str:
        return milestones_i18n.glossary_definition_for(self.term, self.definition)

    @property
    def term_i18n(self) -> str:
        return milestones_i18n.glossary_term_for(self.term, self.term)
