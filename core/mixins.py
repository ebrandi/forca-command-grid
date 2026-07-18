"""Reusable model mixins: timestamps and ESI provenance/freshness."""
from __future__ import annotations

from django.db import models
from django.utils import timezone


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Source(models.TextChoices):
    ESI_CHAR = "esi_char", "ESI (character token)"
    ESI_CORP = "esi_corp", "ESI (corporation/Director token)"
    MANUAL = "manual", "Manual entry"
    ZKILL = "zkill", "zKillboard"
    KILLSTREAM = "killstream", "zKillboard R2Z2 (realtime fallback)"
    EVEREF = "everef", "EVE Ref"
    SDE = "sde", "Static Data Export"
    ESTIMATED = "estimated", "Estimated"
    SYSTEM = "system", "System"


class ProvenanceMixin(models.Model):
    """Marks where a record came from and how fresh it is.

    Every ESI-derived model carries these so the UI can show "as of" and provenance
    honestly. See handbooks/contributor-handbook/security-guidelines.md and
    handbooks/contributor-handbook/domain-model.md.
    """

    source = models.CharField(max_length=16, choices=Source.choices, default=Source.MANUAL)
    as_of = models.DateTimeField(default=timezone.now)
    fetched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True
