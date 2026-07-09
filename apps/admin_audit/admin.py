from __future__ import annotations

from django.contrib import admin

from .models import AppSetting, AuditLog, DataRetentionPolicy


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Append-only: read-only in the admin."""

    list_display = ("created_at", "action", "actor", "target_type", "target_id", "ip")
    list_filter = ("action",)
    search_fields = ("action", "target_id")
    readonly_fields = ("actor", "action", "target_type", "target_id", "metadata", "ip", "created_at")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(AppSetting)
class AppSettingAdmin(admin.ModelAdmin):
    list_display = ("key", "updated_at", "updated_by")
    search_fields = ("key",)


@admin.register(DataRetentionPolicy)
class DataRetentionPolicyAdmin(admin.ModelAdmin):
    list_display = ("data_class", "retention_days", "on_member_leave", "active")
