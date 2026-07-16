from django.contrib import admin

from .models import StoreConfig, StoreOrder


@admin.register(StoreConfig)
class StoreConfigAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "audience", "doctrine_markup", "hull_markup",
                    "capital_markup", "supercap_markup", "deposit_pct")
    list_filter = ("is_active", "audience")


@admin.register(StoreOrder)
class StoreOrderAdmin(admin.ModelAdmin):
    list_display = ("__str__", "kind", "hull_class", "quantity", "total_price", "status", "created_at")
    list_filter = ("status", "kind", "hull_class")
    search_fields = ("ship_name", "fit_name", "location_name")
