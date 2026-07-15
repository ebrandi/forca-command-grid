"""Corporation task management: the execution backbone (PRD Module Q).

Every corp gap can become an owner-sized task here — assigned to a pilot or
left open for anyone to claim — and completing one credits the doer through
the contribution ledger. Tasks can link to the doctrine/operation/shopping
list/build job they serve, so closing a task updates the thing it was for.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from core.mixins import TimeStampedModel


class Task(TimeStampedModel):
    class Type(models.TextChoices):
        BUILD = "build", _("Build ships")
        BUY = "buy", _("Buy modules")
        MOVE = "move", _("Move assets")
        SEED = "seed", _("Seed market")
        TRAIN = "train", _("Train into doctrine")
        REPLACE = "replace", _("Replace losses")
        PREPARE = "prepare", _("Prepare for fleet")
        REVIEW_FIT = "review_fit", _("Review fit")
        MINING = "mining", _("Join mining op")
        DELIVER = "deliver", _("Deliver materials")
        OTHER = "other", _("Other")

    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        CLAIMED = "claimed", _("Claimed")
        IN_PROGRESS = "in_progress", _("In progress")
        DONE = "done", _("Done")
        CANCELLED = "cancelled", _("Cancelled")

    type = models.CharField(max_length=12, choices=Type.choices, default=Type.OTHER, db_index=True)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    priority = models.IntegerField(default=0, help_text=_("Higher = more urgent."))
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
    is_open = models.BooleanField(default=True, help_text=_("Claimable by anyone when unassigned."))
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


# The changed-field slugs stored inside an ``Edited:`` note map to translatable
# labels. gettext_lazy is correct here (dict built once at import time); the join
# in ``note_i18n`` coerces each proxy to str under the reader's request locale.
_EDITED_FIELD_LABELS = {
    "title": _("title"),
    "priority": _("priority"),
    "due_date": _("due date"),
}


class TaskEvent(models.Model):
    """Audit trail of a task's status transitions."""

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    from_status = models.CharField(max_length=12, blank=True)
    to_status = models.CharField(max_length=12)
    # SDE-2 (3.7): a note for non-status events (edit / reassign). Stored as a
    # stable machine code — ``Edited:<slug,slug>`` / ``Unassigned`` /
    # ``Reassigned:<username>`` — NOT display prose, so ``note_i18n`` can re-render
    # it under each reader's locale instead of freezing the writer's English.
    note = models.CharField(max_length=200, blank=True, default="")
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-at"]

    @property
    def note_i18n(self) -> str:
        """Re-render the audit note under the reader's locale.

        New rows store a machine code; legacy rows hold frozen English prose and
        fall through to render verbatim (unknown format), so nothing goes blank.
        """
        note = self.note or ""
        if not note:
            return note
        if note == "Unassigned":
            return gettext("Unassigned")
        if note.startswith("Edited:"):
            slugs = [s for s in note[len("Edited:"):].split(",") if s]
            fields = ", ".join(str(_EDITED_FIELD_LABELS.get(s, s)) for s in slugs)
            return gettext("Edited %(fields)s") % {"fields": fields}
        if note.startswith("Reassigned:"):
            return gettext("Reassigned to %(user)s") % {"user": note[len("Reassigned:"):]}
        return note  # legacy / unknown prose — render verbatim

    @property
    def from_status_label(self) -> str:
        """Translated label for the originating status (blank if unset)."""
        if not self.from_status:
            return ""
        try:
            return Task.Status(self.from_status).label
        except ValueError:
            return self.from_status

    @property
    def to_status_label(self) -> str:
        """Translated label for the destination status."""
        try:
            return Task.Status(self.to_status).label
        except ValueError:
            return self.to_status
