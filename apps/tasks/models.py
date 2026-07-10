"""Corporation task management: the execution backbone (PRD Module Q).

Every corp gap can become an owner-sized task here — assigned to a pilot or
left open for anyone to claim — and completing one credits the doer through
the contribution ledger. Tasks can link to the doctrine/operation/shopping
list/build job they serve, so closing a task updates the thing it was for.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from core.mixins import TimeStampedModel


class Task(TimeStampedModel):
    class Type(models.TextChoices):
        BUILD = "build", "Build ships"
        BUY = "buy", "Buy modules"
        MOVE = "move", "Move assets"
        SEED = "seed", "Seed market"
        TRAIN = "train", "Train into doctrine"
        REPLACE = "replace", "Replace losses"
        PREPARE = "prepare", "Prepare for fleet"
        REVIEW_FIT = "review_fit", "Review fit"
        MINING = "mining", "Join mining op"
        DELIVER = "deliver", "Deliver materials"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLAIMED = "claimed", "Claimed"
        IN_PROGRESS = "in_progress", "In progress"
        DONE = "done", "Done"
        CANCELLED = "cancelled", "Cancelled"

    type = models.CharField(max_length=12, choices=Type.choices, default=Type.OTHER, db_index=True)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    priority = models.IntegerField(default=0, help_text="Higher = more urgent.")
    due_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    # An assignee owns the task; an open task with no assignee is claimable by
    # anyone in the corp.
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="assigned_tasks",
    )
    is_open = models.BooleanField(default=True, help_text="Claimable by anyone when unassigned.")
    # Optional link to the thing this task serves (doctrine/operation/...).
    related_type = models.CharField(max_length=32, blank=True)
    related_id = models.CharField(max_length=64, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="created_tasks",
    )

    class Meta:
        ordering = ["-priority", "due_at", "-created_at"]
        indexes = [
            models.Index(fields=["status", "is_open"]),
            models.Index(fields=["assignee", "status"]),
            # Every soft-linked lookup (campaign objective ↔ task roll-up, volunteer idempotency)
            # filters on the related ref; without this it sequential-scans a growing table.
            models.Index(fields=["related_type", "related_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.type}:{self.title}"

    @property
    def is_active(self) -> bool:
        return self.status in (self.Status.OPEN, self.Status.CLAIMED, self.Status.IN_PROGRESS)


class TaskEvent(models.Model):
    """Audit trail of a task's status transitions."""

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    from_status = models.CharField(max_length=12, blank=True)
    to_status = models.CharField(max_length=12)
    # SDE-2 (3.7): a human note for non-status events (edit / reassign).
    note = models.CharField(max_length=200, blank=True, default="")
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-at"]
