from __future__ import annotations

from django.contrib import admin

from .models import MarketLocation, MarketOrderSnapshot, MarketPrice


@admin.register(MarketLocation)
class MarketLocationAdmin(admin.ModelAdmin):
    list_display = ("name", "location_type", "is_price_reference", "is_staging", "active")


@admin.register(MarketPrice)
class MarketPriceAdmin(admin.ModelAdmin):
    list_display = ("type_id", "location", "profile", "sell_min", "buy_max", "as_of")
    list_filter = ("profile",)


admin.site.register(MarketOrderSnapshot)
