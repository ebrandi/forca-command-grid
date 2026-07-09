"""Knowledge base (PRD Module R): versioned corp docs with live embeds.

Pages are markdown with a visibility tier and a revision history. A small set of
allowlisted embed tags render live, viewer-scoped data (the reader's own
readiness/SRP), so guides never go stale.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from core.mixins import TimeStampedModel


class KbPage(TimeStampedModel):
    class Visibility(models.TextChoices):
        PUBLIC = "public", "Public"
        MEMBER = "member", "Members"
        OFFICER = "officer", "Officers"

    slug = models.SlugField(max_length=80, unique=True)
    title = models.CharField(max_length=200)
    category = models.CharField(max_length=60, blank=True)
    visibility = models.CharField(
        max_length=8, choices=Visibility.choices, default=Visibility.MEMBER, db_index=True
    )
    body_md = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["category", "title"]

    def __str__(self) -> str:
        return self.title


class KbRevision(models.Model):
    page = models.ForeignKey(KbPage, on_delete=models.CASCADE, related_name="revisions")
    body_md = models.TextField(blank=True)
    edited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    edited_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-edited_at"]
