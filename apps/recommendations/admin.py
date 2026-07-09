from __future__ import annotations

from django.contrib import admin

from .models import ActionQueueItem, Alert, Recommendation


@admin.register(Recommendation)
class RecommendationAdmin(admin.ModelAdmin):
    list_display = ("type", "subject_type", "subject_id", "confidence", "severity", "state", "created_at")
    list_filter = ("type", "state", "confidence")


@admin.register(ActionQueueItem)
class ActionQueueItemAdmin(admin.ModelAdmin):
    list_display = ("recommendation", "assigned_to", "status", "updated_at")
    list_filter = ("status",)


admin.site.register(Alert)
