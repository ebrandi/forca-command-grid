from django.contrib import admin

from .models import (
    FitOffer,
    FitReservation,
    FitStock,
    FitStockEntry,
    FitSupplyNeed,
    ShipyardPolicy,
    StoreConfig,
    StoreOrder,
)


@admin.register(StoreConfig)
class StoreConfigAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "audience", "doctrine_markup", "hull_markup",
                    "capital_markup", "supercap_markup", "deposit_pct")
    list_filter = ("is_active", "audience")


@admin.register(StoreOrder)
class StoreOrderAdmin(admin.ModelAdmin):
    list_display = ("__str__", "kind", "hull_class", "quantity", "quantity_reserved",
                    "quantity_backordered", "total_price", "status", "created_at")
    list_filter = ("status", "kind", "hull_class", "availability_state")
    search_fields = ("ship_name", "fit_name", "location_name")


@admin.register(ShipyardPolicy)
class ShipyardPolicyAdmin(admin.ModelAdmin):
    list_display = ("__str__", "is_active", "backorders_enabled", "default_lead_days",
                    "max_order_quantity", "reservation_expiry_days")
    list_filter = ("is_active",)


@admin.register(FitOffer)
class FitOfferAdmin(admin.ModelAdmin):
    list_display = ("fit", "is_offered", "backorders_allowed", "lead_days",
                    "delivery_location", "target_stock", "priority")
    list_filter = ("is_offered", "preferred_fulfilment")
    search_fields = ("fit__name",)
    raw_id_fields = ("fit",)


@admin.register(FitStock)
class FitStockAdmin(admin.ModelAdmin):
    list_display = ("doctrine_fit", "location", "quantity_on_hand",
                    "manifest_hash", "last_reconciled_at")
    search_fields = ("doctrine_fit__name",)
    raw_id_fields = ("doctrine_fit",)


@admin.register(FitStockEntry)
class FitStockEntryAdmin(admin.ModelAdmin):
    """The ledger is immutable — read-only in the Django admin too."""

    list_display = ("stock", "kind", "delta", "balance_after", "actor", "order", "created_at")
    list_filter = ("kind",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(FitReservation)
class FitReservationAdmin(admin.ModelAdmin):
    list_display = ("order", "stock", "quantity", "status", "created_at",
                    "released_at", "consumed_at")
    list_filter = ("status",)
    raw_id_fields = ("order", "stock")


@admin.register(FitSupplyNeed)
class FitSupplyNeedAdmin(admin.ModelAdmin):
    list_display = ("doctrine_fit", "location", "status", "quantity_required",
                    "required_by", "industry_project", "build_job", "task")
    list_filter = ("status",)
    raw_id_fields = ("doctrine_fit", "industry_project", "build_job", "task")
