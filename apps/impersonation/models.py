"""Director "view-as" impersonation — durable audit records.

An :class:`ImpersonationSession` is the first-class, queryable trail of every time a
director viewed the site as another pilot (support troubleshooting). It complements the
immutable ``core.audit`` AuditLog rows (``impersonation.start`` / ``impersonation.end`` /
``impersonation.write_blocked``) with a listable object the ops console surfaces so
leadership can hold impersonation to account.

Impersonation is strictly VIEW-ONLY — state-changing requests are refused by
:mod:`apps.impersonation.middleware` — and can only ever target a pilot strictly below
the actor's role rank and never a superuser (see :mod:`apps.impersonation.policy`).
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone


class ImpersonationSession(models.Model):
    """One director "view-as" session: who, as whom, when it started/ended, why it ended."""

    class EndReason(models.TextChoices):
        ACTIVE = "", "Active"
        MANUAL = "manual", "Exited by director"
        EXPIRED = "expired", "Auto-expired (max duration)"
        ACTOR_INVALID = "actor_invalid", "Director no longer authorised"
        TARGET_INVALID = "target_invalid", "Target no longer impersonatable"
        LOGOUT = "logout", "Director logged out"

    # SET_NULL + snapshot labels so the audit trail survives an account
    # deletion / GDPR erasure of either party.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="impersonations_made",
    )
    target = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="impersonated_sessions",
    )
    actor_label = models.CharField(max_length=200, blank=True)
    target_label = models.CharField(max_length=200, blank=True)
    # Optional operator-supplied justification captured at start (aids the audit trail).
    reason = models.CharField(max_length=200, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(
        max_length=16, choices=EndReason.choices, default=EndReason.ACTIVE, blank=True
    )

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["ended_at", "started_at"])]

    def __str__(self) -> str:
        return f"{self.actor_label or self.actor_id} → {self.target_label or self.target_id}"

    @property
    def is_open(self) -> bool:
        """Row has not been explicitly closed (may still be abandoned — see the log view)."""
        return self.ended_at is None

    @property
    def duration(self):
        return (self.ended_at or timezone.now()) - self.started_at
