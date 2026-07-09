"""Skill Planning: ordered training plans toward doctrine goals."""
from __future__ import annotations

from django.db import models

from apps.doctrines.models import Doctrine
from apps.sso.models import EveCharacter
from core.mixins import TimeStampedModel


class SkillPlan(TimeStampedModel):
    class Goal(models.TextChoices):
        DOCTRINE = "doctrine", "Doctrine"
        NEWBRO = "newbro", "Newbro"
        CUSTOM = "custom", "Custom"

    character = models.ForeignKey(EveCharacter, on_delete=models.CASCADE, related_name="skill_plans")
    name = models.CharField(max_length=200)
    target_doctrine = models.ForeignKey(
        Doctrine, on_delete=models.SET_NULL, null=True, blank=True, related_name="skill_plans"
    )
    goal = models.CharField(max_length=12, choices=Goal.choices, default=Goal.CUSTOM)
    corp_priority_weighted = models.BooleanField(default=False)
    estimated_total_seconds = models.BigIntegerField(null=True, blank=True)

    def __str__(self) -> str:
        return self.name


class SkillPlanStep(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        TRAINING = "training", "Training"
        DONE = "done", "Done"

    plan = models.ForeignKey(SkillPlan, on_delete=models.CASCADE, related_name="steps")
    order = models.IntegerField(default=0)
    skill_type_id = models.IntegerField()
    target_level = models.PositiveSmallIntegerField()
    estimated_seconds = models.BigIntegerField(null=True, blank=True)
    reason = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)

    class Meta:
        ordering = ["order"]


class IdleQueueNudge(models.Model):
    """Tracks whether an idle-queue nudge was already sent for a character's *current*
    idle period, so an opted-in pilot gets one reminder per empty→ transition, not
    repeated pings. A row exists only while a character is idle-and-notified; it is
    deleted when the queue is next seen training, so a later idle period nudges again
    (its fresh row's PK also keys the alert's idempotency, preventing a duplicate)."""

    character_id = models.BigIntegerField(unique=True)
    notified_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"idle-nudge {self.character_id} @ {self.notified_at}"
