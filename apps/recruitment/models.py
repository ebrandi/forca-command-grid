"""Recruitment intelligence (PRD Module P).

A recruiter's evidence desk. The MVP works on PUBLIC data only (corp history,
character age, killboard) and presents evidence with confidence levels — never
an automated accept/reject. ``CandidateConsent`` models the optional, time-boxed
ESI link a candidate may authorise; the live token exchange is a separately
reviewed step (it is the highest-trust-risk surface in the product).

Privacy: we store *derived claims*, never a candidate's raw inventory; rejected
candidates' data is purged on a short timer; all recruiter access is audit-logged.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.mixins import TimeStampedModel


class Candidate(TimeStampedModel):
    class Status(models.TextChoices):
        PROSPECT = "prospect", _("Prospect")
        LINKED = "linked", _("ESI linked")
        JOINED = "joined", _("Joined")
        REJECTED = "rejected", _("Rejected")

    character_id = models.BigIntegerField(unique=True)
    name = models.CharField(max_length=120)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PROSPECT)
    notes = models.TextField(blank=True)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    evidence_refreshed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name


class CandidateEvidence(TimeStampedModel):
    class Theme(models.TextChoices):
        IDENTITY = "identity", _("Identity")
        COMBAT = "combat", _("Combat")
        INDUSTRY = "industry", _("Industry")
        ROLES = "roles", _("Roles")
        TIMEZONE = "timezone", _("Timezone")
        RISK = "risk", _("Risk")

    class Confidence(models.TextChoices):
        HIGH = "high", _("High")
        MEDIUM = "medium", _("Medium")
        LOW = "low", _("Low")

    candidate = models.ForeignKey(Candidate, on_delete=models.CASCADE, related_name="evidence")
    theme = models.CharField(max_length=12, choices=Theme.choices)
    # Free-text display claim — can be long (e.g. a Director's full corp-roles list), so
    # not length-capped: a 300-char CharField overflowed on a many-roles candidate.
    claim = models.TextField()
    confidence = models.CharField(max_length=8, choices=Confidence.choices, default=Confidence.MEDIUM)
    source = models.CharField(max_length=8, default="public")
    is_flag = models.BooleanField(default=False)

    class Meta:
        ordering = ["theme", "-confidence"]


class CandidateConsent(TimeStampedModel):
    """A requested/granted ESI consent — scoped, time-boxed, revocable.

    We deliberately do not store the candidate's access token long-term: the
    consent is used once to derive claims, then only the derived claims remain.
    """

    candidate = models.ForeignKey(Candidate, on_delete=models.CASCADE, related_name="consents")
    scopes = models.JSONField(default=list)
    state = models.CharField(max_length=64, unique=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    expires_at = models.DateTimeField()
    granted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    @property
    def is_active(self) -> bool:
        from django.utils import timezone

        return self.revoked_at is None and self.expires_at > timezone.now()
