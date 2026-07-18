"""Tocha's Lab persistence: fits, immutable revisions, and damage profiles.

Design rules (see docs/architecture/decisions/tochas-lab-fitting-engine.md §10):
* A :class:`Fit` is the mutable envelope (name, owner, visibility, lineage). Its *content*
  lives in immutable :class:`FitRevision` snapshots — saving never mutates a prior revision,
  so a shared or doctrine-referenced revision can never change under anyone.
* Calculated telemetry is NEVER stored as authoritative state; it is always reproducible
  from a revision + skill/operating/damage profile + engine & data versions (all recorded).
* Public links use an unguessable token, are revocable, and never expose sequential ids.
"""
from __future__ import annotations

import secrets

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.mixins import TimeStampedModel


def new_share_token() -> str:
    """A URL-safe, unguessable public-share id (≈128 bits). Never sequential."""
    return secrets.token_urlsafe(16)


class Visibility(models.TextChoices):
    PRIVATE = "private", _("Private")
    CORPORATION = "corporation", _("Corporation")
    ALLIANCE = "alliance", _("Alliance")
    PUBLIC = "public", _("Public link")
    DOCTRINE = "doctrine", _("Doctrine-managed")


class Fit(TimeStampedModel):
    """A saved fit: the mutable envelope around an append-only revision history."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tochas_fits"
    )
    name = models.CharField(max_length=200)
    ship_type_id = models.IntegerField(db_index=True)
    description = models.TextField(blank=True)
    activity = models.CharField(max_length=48, blank=True)      # intended activity
    environment = models.CharField(max_length=48, blank=True)   # intended environment
    tags = models.JSONField(default=list, blank=True)
    visibility = models.CharField(
        max_length=12, choices=Visibility.choices, default=Visibility.PRIVATE
    )
    # Unguessable public-link id; null unless the owner has published a link.
    share_token = models.CharField(max_length=32, unique=True, null=True, blank=True)
    share_revoked = models.BooleanField(default=False)
    is_archived = models.BooleanField(default=False)
    # Fork lineage — where this fit came from (a doctrine fit, another fit, a killmail…).
    forked_from = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="forks"
    )
    forked_from_revision = models.PositiveIntegerField(null=True, blank=True)
    origin = models.CharField(max_length=24, blank=True)  # e.g. scratch|eft|killmail|doctrine|pilot
    # Set once this fit's current revision has been promoted into a doctrine.
    promoted_doctrine_fit_id = models.IntegerField(null=True, blank=True)
    current_revision = models.ForeignKey(
        "FitRevision", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["owner", "is_archived"]),
            models.Index(fields=["visibility"]),
        ]

    def __str__(self) -> str:
        return self.name

    # --- visibility helpers (server-side authorisation) --------------------- #
    def can_view(self, user) -> bool:
        """Whether ``user`` may view this fit. Public-link access is handled by the
        share-token view, NOT here — a revoked/absent token must never resolve by id."""
        if self.owner_id == getattr(user, "pk", None):
            return True
        if getattr(user, "is_superuser", False):
            return True
        from core import rbac

        if self.visibility == Visibility.CORPORATION:
            return rbac.has_role(user, rbac.ROLE_MEMBER)
        if self.visibility in (Visibility.DOCTRINE, Visibility.ALLIANCE):
            return rbac.has_role(user, rbac.ROLE_MEMBER)
        return False

    def can_edit(self, user) -> bool:
        return self.owner_id == getattr(user, "pk", None) or getattr(user, "is_superuser", False)

    @property
    def public_link_active(self) -> bool:
        return bool(self.share_token) and not self.share_revoked


class FitRevision(models.Model):
    """An immutable content snapshot of a fit at one point in time.

    ``items`` is the canonical, engine-ready list of
    ``{"type_id", "slot", "state", "charge_type_id", "quantity"}``. Never mutated after
    creation — a change to a fit appends a new revision.
    """

    fit = models.ForeignKey(Fit, on_delete=models.CASCADE, related_name="revisions")
    revision_number = models.PositiveIntegerField()
    ship_type_id = models.IntegerField()
    items = models.JSONField(default=list)
    notes = models.TextField(blank=True)
    change_summary = models.CharField(max_length=280, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # Provenance for safe recalculation after EVE data updates (§10).
    engine_version = models.CharField(max_length=16, blank=True)
    data_version = models.CharField(max_length=32, blank=True)

    class Meta:
        ordering = ["-revision_number"]
        constraints = [
            models.UniqueConstraint(fields=["fit", "revision_number"], name="uniq_fit_revision"),
        ]

    def __str__(self) -> str:
        return f"{self.fit_id}#{self.revision_number}"


class DamageProfile(TimeStampedModel):
    """A saved incoming-damage profile. ``owner`` null => a corp-wide profile."""

    name = models.CharField(max_length=80)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
        related_name="tochas_damage_profiles",
    )
    em = models.FloatField(default=25.0)
    thermal = models.FloatField(default=25.0)
    kinetic = models.FloatField(default=25.0)
    explosive = models.FloatField(default=25.0)
    is_corp_default = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def total(self) -> float:
        return self.em + self.thermal + self.kinetic + self.explosive
