from __future__ import annotations

from django.contrib import admin

from .models import (
    Blueprint,
    IndustryEconomyConfig,
    IndustryProject,
    IndustryProjectItem,
)


class IndustryProjectItemInline(admin.TabularInline):
    model = IndustryProjectItem
    extra = 0


@admin.register(IndustryProject)
class IndustryProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "objective_type", "status", "visibility", "assigned_to", "is_archived", "due_at")
    list_filter = ("objective_type", "status", "visibility", "is_archived", "source")
    inlines = [IndustryProjectItemInline]


@admin.register(IndustryEconomyConfig)
class IndustryEconomyConfigAdmin(admin.ModelAdmin):
    list_display = ("__str__", "is_active", "default_sales_tax", "default_broker_fee", "erp_redirects")


admin.site.register(Blueprint)
