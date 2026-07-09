from django.contrib import admin

from .models import CourierContract, RateCard


@admin.register(RateCard)
class RateCardAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "discount", "min_reward", "updated_at")
    list_filter = ("is_active",)


@admin.register(CourierContract)
class CourierContractAdmin(admin.ModelAdmin):
    list_display = ("__str__", "ship_class", "jumps", "volume_m3", "collateral", "reward", "status")
    list_filter = ("status", "ship_class", "sec_band")
    search_fields = ("origin_name", "dest_name", "customer", "contact")
