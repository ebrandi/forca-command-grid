"""Admin & Audit: append-only audit log, app settings, retention policy."""
from __future__ import annotations

from django.conf import settings
from django.db import models

from core.mixins import TimeStampedModel


class AuditLog(models.Model):
    """Immutable record of a sensitive action (officer member-data access,
    scope grants, role changes, deletions). Never updated or deleted in app."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    action = models.CharField(max_length=128, db_index=True)
    target_type = models.CharField(max_length=64, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action}"


class AppSetting(TimeStampedModel):
    """Runtime configuration (feature flags, freshness thresholds, compat date)."""

    key = models.CharField(max_length=64, unique=True)
    value = models.JSONField(default=dict, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    def __str__(self) -> str:
        return self.key

    @classmethod
    def get(cls, key: str, default=None):
        row = cls.objects.filter(key=key).first()
        return row.value if row else default


class DataRetentionPolicy(TimeStampedModel):
    class DataClass(models.TextChoices):
        SKILL_SNAPSHOT = "skill_snapshot", "Skill snapshot"
        ASSET_SNAPSHOT = "asset_snapshot", "Asset snapshot"
        TOKEN = "token", "OAuth token"
        AUDIT = "audit", "Audit log"
        MARKET_SNAPSHOT = "market_snapshot", "Market snapshot"

    class OnLeave(models.TextChoices):
        DELETE = "delete", "Delete"
        ANONYMISE = "anonymise", "Anonymise"
        RETAIN = "retain", "Retain"

    data_class = models.CharField(max_length=32, choices=DataClass.choices, unique=True)
    retention_days = models.PositiveIntegerField(default=365)
    on_member_leave = models.CharField(
        max_length=16, choices=OnLeave.choices, default=OnLeave.DELETE
    )
    active = models.BooleanField(default=True)

    def __str__(self) -> str:
        return f"{self.data_class} ({self.retention_days}d)"
