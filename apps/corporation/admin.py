from django.contrib import admin, messages

from .models import CorpMember, CorpStructure, FriendlyCorporation, PartnerAlliance


@admin.register(CorpStructure)
class CorpStructureAdmin(admin.ModelAdmin):
    list_display = ("name", "type_name", "system_name", "state", "fuel_expires")
    list_filter = ("state",)
    search_fields = ("name", "system_name")


@admin.register(CorpMember)
class CorpMemberAdmin(admin.ModelAdmin):
    list_display = ("name", "character_id", "ship_type_id", "logon_date", "logoff_date")
    search_fields = ("name", "character_id")


@admin.register(PartnerAlliance)
class PartnerAllianceAdmin(admin.ModelAdmin):
    """Register extra alliances that get the same alliance-service access as our own."""

    list_display = ("alliance_id", "name", "active", "note")
    list_editable = ("active",)
    list_filter = ("active",)
    search_fields = ("alliance_id", "name")
    fields = ("alliance_id", "name", "note", "active")
    actions = ["resolve_names"]

    @admin.action(description="Resolve alliance names from ESI")
    def resolve_names(self, request, queryset):
        """Best-effort fill of the ``name`` label from ESI for the selected rows."""
        from core.esi.names import resolve_ids

        from .models import EveName

        ids = list(queryset.values_list("alliance_id", flat=True))
        try:
            resolve_ids(ids)
        except Exception as exc:  # noqa: BLE001 - ESI is best-effort here
            self.message_user(request, f"ESI name lookup failed: {exc}", level=messages.WARNING)
            return
        names = dict(EveName.objects.filter(entity_id__in=ids).values_list("entity_id", "name"))
        updated = 0
        for partner in queryset:
            resolved = names.get(partner.alliance_id)
            if resolved and resolved != partner.name:
                partner.name = resolved
                partner.save(update_fields=["name"])
                updated += 1
        self.message_user(request, f"Resolved {updated} alliance name(s).")


@admin.register(FriendlyCorporation)
class FriendlyCorporationAdmin(admin.ModelAdmin):
    """Break-glass parity with the Access-governance console page; corps get the same
    alliance-service access as our own alliance."""

    list_display = ("corporation_id", "name", "active", "note")
    list_editable = ("active",)
    list_filter = ("active",)
    search_fields = ("corporation_id", "name")
    fields = ("corporation_id", "name", "note", "active")
