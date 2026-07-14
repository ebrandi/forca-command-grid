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
from django.db.models import JSONField, Value
from django.utils.translation import gettext_lazy as _

from core.mixins import TimeStampedModel

from .messages import render_text


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
    #
    # The prose column STAYS. It is the English fallback, the audit record of what the recruiter
    # was actually shown, and what every legacy row (written before the columns below landed)
    # renders from. Nothing is backfilled — a keyless row degrades to its stored English, never
    # to blank.
    claim = models.TextField()
    # Seam B (see ``messages.py``): this row is written by a Celery worker that has no reader and
    # no locale, so ``claim`` above can only ever be frozen English. These carry the scaffold key
    # plus its plain-JSON params so ``claim_i18n`` can re-render the sentence under the READER's
    # locale. ``db_default`` (not merely ``default``) is load-bearing — see the migration.
    claim_key = models.CharField(max_length=60, blank=True, default="", db_default="")
    claim_params = models.JSONField(blank=True, default=dict, db_default=Value({}, JSONField()))
    confidence = models.CharField(max_length=8, choices=Confidence.choices, default=Confidence.MEDIUM)
    source = models.CharField(max_length=8, default="public")
    is_flag = models.BooleanField(default=False)

    class Meta:
        ordering = ["theme", "-confidence"]

    # --- Seam B read side: resolve under the READER's locale, never the writer's -------------
    @property
    def claim_i18n(self) -> str:
        """The claim in the reader's active language; the stored English when there is no key.

        Can never return blank: ``render_text`` falls back to ``self.claim``, which every row has.
        """
        return render_text(self.claim_key, self.claim_params, self.claim)


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
