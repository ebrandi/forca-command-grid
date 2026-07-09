from __future__ import annotations

from django.contrib import admin

from .models import GlossaryTerm, OnboardingMilestone, OnboardingProgress


@admin.register(OnboardingMilestone)
class OnboardingMilestoneAdmin(admin.ModelAdmin):
    list_display = ("key", "title", "category", "sort_order", "active")


@admin.register(GlossaryTerm)
class GlossaryTermAdmin(admin.ModelAdmin):
    list_display = ("term",)
    search_fields = ("term",)


admin.site.register(OnboardingProgress)
