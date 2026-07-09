from __future__ import annotations

from django.contrib import admin

from .models import BattleReport, Killmail, Watchlist, WatchlistEntry


@admin.register(Killmail)
class KillmailAdmin(admin.ModelAdmin):
    list_display = (
        "killmail_id",
        "killmail_time",
        "victim_ship_type_id",
        "solar_system_id",
        "total_value",
        "points",
        "involves_home_corp",
        "home_corp_role",
    )
    list_filter = ("involves_home_corp", "home_corp_role", "sec_band", "is_solo")
    search_fields = ("killmail_id", "victim_character_id", "victim_corporation_id")


class WatchlistEntryInline(admin.TabularInline):
    model = WatchlistEntry
    extra = 0


@admin.register(Watchlist)
class WatchlistAdmin(admin.ModelAdmin):
    list_display = ("name", "purpose")
    inlines = [WatchlistEntryInline]


admin.site.register(BattleReport)
