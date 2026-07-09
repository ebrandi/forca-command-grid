from django.contrib import admin

from .models import ContributionEvent, PilotPreference


@admin.register(PilotPreference)
class PilotPreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "public_recognition", "primary_character_id", "timezone")
    search_fields = ("user__username",)


@admin.register(ContributionEvent)
class ContributionEventAdmin(admin.ModelAdmin):
    list_display = ("user", "kind", "magnitude", "unit", "occurred_at")
    list_filter = ("kind",)
    search_fields = ("user__username", "description")
