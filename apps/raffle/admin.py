"""Break-glass Django admin registrations (Django /admin is disabled in prod;
leadership uses the /ops/admin/raffle/ console). Kept minimal for dev inspection."""
from django.contrib import admin

from .models import (
    RaffleContest,
    RaffleContestTemplate,
    RaffleDraw,
    RaffleDrawResult,
    RaffleExclusion,
    RaffleIneligibleActivity,
    RaffleManualGrant,
    RaffleParticipantSummary,
    RafflePrize,
    RaffleSuspiciousActivityFlag,
    RaffleTicketLedgerEntry,
    RaffleTicketSourceConfig,
)


@admin.register(RaffleContest)
class RaffleContestAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "start_at", "end_at", "draw_at")
    list_filter = ("status",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(RafflePrize)
class RafflePrizeAdmin(admin.ModelAdmin):
    list_display = ("contest", "rank", "name", "prize_type", "estimated_value")
    list_filter = ("prize_type",)


@admin.register(RaffleTicketSourceConfig)
class RaffleTicketSourceConfigAdmin(admin.ModelAdmin):
    list_display = ("contest", "source_key", "enabled", "mode")
    list_filter = ("enabled", "mode", "source_key")


@admin.register(RaffleTicketLedgerEntry)
class RaffleTicketLedgerEntryAdmin(admin.ModelAdmin):
    list_display = ("contest", "character_name", "source_key", "amount", "status", "created_at")
    list_filter = ("status", "source_key")
    search_fields = ("character_name", "source_ref")


@admin.register(RaffleManualGrant)
class RaffleManualGrantAdmin(admin.ModelAdmin):
    list_display = ("contest", "character_name", "amount", "category", "granted_by", "override_used")
    list_filter = ("override_used", "category")


@admin.register(RaffleDraw)
class RaffleDrawAdmin(admin.ModelAdmin):
    list_display = ("contest", "status", "eligible_pilots", "total_eligible_tickets", "completed_at")
    list_filter = ("status",)


@admin.register(RaffleDrawResult)
class RaffleDrawResultAdmin(admin.ModelAdmin):
    list_display = ("draw", "draw_order", "prize", "winner_character_name", "fulfil_status")
    list_filter = ("fulfil_status", "status")


@admin.register(RaffleParticipantSummary)
class RaffleParticipantSummaryAdmin(admin.ModelAdmin):
    list_display = ("contest", "character_name", "total_tickets", "rank", "eligible")
    list_filter = ("eligible",)


@admin.register(RaffleIneligibleActivity)
class RaffleIneligibleActivityAdmin(admin.ModelAdmin):
    list_display = ("contest", "character_id", "source_key", "reason", "would_be_tickets")
    list_filter = ("reason", "source_key")


@admin.register(RaffleSuspiciousActivityFlag)
class RaffleSuspiciousActivityFlagAdmin(admin.ModelAdmin):
    list_display = ("contest", "character_id", "flag_type", "status")
    list_filter = ("flag_type", "status")


@admin.register(RaffleExclusion)
class RaffleExclusionAdmin(admin.ModelAdmin):
    list_display = ("contest", "character_name", "user", "active")
    list_filter = ("active",)


@admin.register(RaffleContestTemplate)
class RaffleContestTemplateAdmin(admin.ModelAdmin):
    list_display = ("key", "name", "built_in", "active")
    list_filter = ("built_in", "active")
