from django.contrib import admin

from .models import (
    Operation,
    OperationCancellation,
    OperationCommitment,
    OperationDoctrine,
    OperationRsvp,
    OperationShipSlot,
)


class OperationDoctrineInline(admin.TabularInline):
    model = OperationDoctrine
    extra = 1


class OperationShipSlotInline(admin.TabularInline):
    model = OperationShipSlot
    extra = 1


@admin.register(Operation)
class OperationAdmin(admin.ModelAdmin):
    list_display = ("name", "type", "target_at", "status", "min_pilots", "rsvp_deadline")
    list_filter = ("type", "status")
    inlines = [OperationShipSlotInline, OperationDoctrineInline]


@admin.register(OperationCommitment)
class OperationCommitmentAdmin(admin.ModelAdmin):
    list_display = ("operation", "character_name", "slot", "response", "created_at")
    list_filter = ("response",)


@admin.register(OperationCancellation)
class OperationCancellationAdmin(admin.ModelAdmin):
    list_display = ("operation_pk", "operation_type", "reason", "min_pilots",
                    "confirmed_at_deadline", "created_at")
    list_filter = ("reason", "operation_type")
    readonly_fields = [f.name for f in OperationCancellation._meta.fields]


@admin.register(OperationRsvp)
class OperationRsvpAdmin(admin.ModelAdmin):
    list_display = ("operation", "character_name", "response", "updated_at")
    list_filter = ("response",)
