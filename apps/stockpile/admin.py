from __future__ import annotations

from django.contrib import admin

from .models import HaulingTask, Stockpile, StockpileItem, StockReservation


class StockpileItemInline(admin.TabularInline):
    model = StockpileItem
    extra = 0


@admin.register(Stockpile)
class StockpileAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "location", "source", "as_of")
    inlines = [StockpileItemInline]


@admin.register(HaulingTask)
class HaulingTaskAdmin(admin.ModelAdmin):
    list_display = ("id", "type_id", "volume_m3", "source_location", "dest_location", "status")
    list_filter = ("status",)


admin.site.register(StockReservation)
