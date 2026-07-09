from django.contrib import admin

from .models import SrpBudget, SrpClaim, SrpProgram, SrpRule


@admin.register(SrpProgram)
class SrpProgramAdmin(admin.ModelAdmin):
    list_display = ("__str__", "enabled", "payout_mode", "valuation", "require_doctrine")
    list_filter = ("enabled", "payout_mode", "valuation")


@admin.register(SrpRule)
class SrpRuleAdmin(admin.ModelAdmin):
    list_display = ("__str__", "basis", "max_payout", "active")
    list_filter = ("basis", "active")


@admin.register(SrpClaim)
class SrpClaimAdmin(admin.ModelAdmin):
    list_display = ("killmail_id", "claimant", "status", "computed_payout", "doctrine")
    list_filter = ("status", "basis")


@admin.register(SrpBudget)
class SrpBudgetAdmin(admin.ModelAdmin):
    list_display = ("period", "allocated")
