from __future__ import annotations

from django.contrib import admin

from .models import (
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

    def save_model(self, request, obj, form, change):
        """A closed/archived plan holds no ACTIVE reservations (P1) — the admin
        change form must uphold the invariant like every other close path."""
        super().save_model(request, obj, form, change)
        if obj.is_archived or obj.status in (
            IndustryProject.Status.DONE, IndustryProject.Status.CANCELLED
        ):
            from core.audit import audit_log

            from .services import release_project_stock

            released = release_project_stock(obj)
            if released:
                audit_log(
                    getattr(request, "user", None), "industry.release_stock",
                    target_type="industry_project", target_id=str(obj.pk),
                    metadata={"released": released, "reason": "admin_close"},
                )


@admin.register(IndustryEconomyConfig)
class IndustryEconomyConfigAdmin(admin.ModelAdmin):
    list_display = ("__str__", "is_active", "default_sales_tax", "default_broker_fee", "erp_redirects")
