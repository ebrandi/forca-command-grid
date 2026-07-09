from django.contrib import admin

from .models import ReadinessSnapshot


@admin.register(ReadinessSnapshot)
class ReadinessSnapshotAdmin(admin.ModelAdmin):
    list_display = ("created_at", "index")
    readonly_fields = ("dimensions", "coverage")
