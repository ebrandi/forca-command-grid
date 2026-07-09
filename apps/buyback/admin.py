from django.contrib import admin

from .models import BuybackConfig, BuybackOffer


@admin.register(BuybackConfig)
class BuybackConfigAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "audience", "highsec_pct", "lowsec_pct", "nullsec_pct")
    list_filter = ("is_active", "audience")


@admin.register(BuybackOffer)
class BuybackOfferAdmin(admin.ModelAdmin):
    list_display = ("__str__", "seller", "buyer", "sec_band", "offer_total", "status", "created_at")
    list_filter = ("status", "sec_band")
    search_fields = ("location_name", "notes")
