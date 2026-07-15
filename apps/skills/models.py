"""Skill Planning: ordered training plans toward doctrine goals."""
from __future__ import annotations

from django.db import models
from django.utils.translation import gettext, gettext_lazy as _

from apps.doctrines.models import Doctrine
from apps.sso.models import EveCharacter
from core.mixins import TimeStampedModel


class SkillPlan(TimeStampedModel):
    class Goal(models.TextChoices):
        # "Doctrine" and "Newbro" are EVE community jargon — left English on purpose.
        DOCTRINE = "doctrine", "Doctrine"
        NEWBRO = "newbro", "Newbro"
        CUSTOM = "custom", _("Custom")

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

    @property
    def name_i18n(self) -> str:
        """The plan name to *display*, localised per reader locale.

        Doctrine plans are auto-named ``"Fly <doctrine>"`` in the creator's locale at
        generation time (:func:`apps.skills.services.generate_plan_for_doctrine`) and that
        English is frozen into ``name``. Rebuild the label per reader from the still-stored
        ``target_doctrine`` FK; the doctrine name itself is an EVE proper noun and stays
        verbatim. Custom/newbro plans (and doctrine plans whose FK was cleared) carry
        officer/system free text and are returned verbatim.
        """
        if self.goal == self.Goal.DOCTRINE and self.target_doctrine_id:
            return gettext("Fly %(doctrine)s") % {"doctrine": self.target_doctrine.name}
        return self.name


class SkillPlanStep(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        TRAINING = "training", _("Training")
        DONE = "done", _("Done")

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
