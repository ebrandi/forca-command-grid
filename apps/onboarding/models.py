"""New Player Onboarding: milestones, progress, glossary."""
from __future__ import annotations

from django.db import models

from apps.sso.models import EveCharacter


class OnboardingMilestone(models.Model):
    class Category(models.TextChoices):
        ACCOUNT = "account", "Account"
        SKILLS = "skills", "Skills"
        DOCTRINE = "doctrine", "Doctrine"
        ACTIVITY = "activity", "Activity"

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


class OnboardingProgress(models.Model):
    class Status(models.TextChoices):
        TODO = "todo", "To do"
        IN_PROGRESS = "in_progress", "In progress"
        DONE = "done", "Done"

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
