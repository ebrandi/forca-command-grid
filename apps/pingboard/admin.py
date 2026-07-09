"""Django-admin registration — read-mostly. Secrets are never surfaced."""
from __future__ import annotations

from django.contrib import admin

from .models import (
    Alert,
    AlertDelivery,
    AlertRecipient,
    AlertTemplate,
    AutomationRule,
    CalendarEvent,
    CalendarEventAlert,
    CalendarSyncEvent,
    ChannelProvider,
    PilotContactChannel,
)


@admin.register(AutomationRule)
class AutomationRuleAdmin(admin.ModelAdmin):
    list_display = ("label", "trigger_source", "category", "enabled", "priority", "last_fired_at")
    list_filter = ("enabled", "trigger_source", "category")
    search_fields = ("key", "label", "trigger_source")


@admin.register(ChannelProvider)
class ChannelProviderAdmin(admin.ModelAdmin):
    # NB: never put the secret (``_secret``/``secret``) in list_display or fields —
    # webhook URLs / tokens must not leak through the admin.
    list_display = ("label", "kind", "enabled", "is_default", "is_emergency", "has_secret", "last_ok_at")
    list_filter = ("kind", "enabled")
    readonly_fields = ("last_ok_at", "last_error", "last_error_at", "last_test_at", "has_secret")
    exclude = ("_secret",)


@admin.register(AlertTemplate)
class AlertTemplateAdmin(admin.ModelAdmin):
    list_display = ("label", "key", "category", "default_priority", "is_official", "enabled")
    list_filter = ("category", "enabled", "is_official")
    search_fields = ("key", "label")


class AlertDeliveryInline(admin.TabularInline):
    model = AlertDelivery
    extra = 0
    readonly_fields = ("kind", "provider", "status", "attempts", "provider_message_id", "last_error")


class AlertRecipientInline(admin.TabularInline):
    model = AlertRecipient
    extra = 0
    readonly_fields = ("kind", "recipient_type", "recipient_ref", "status", "error")


@admin.register(PilotContactChannel)
class PilotContactChannelAdmin(admin.ModelAdmin):
    # 'handle' is PII (phone / chat id) — kept out of the list view.
    list_display = ("user", "kind", "verified", "verified_at")
    list_filter = ("kind", "verified")


class CalendarEventAlertInline(admin.TabularInline):
    model = CalendarEventAlert
    extra = 0


@admin.register(CalendarEvent)
class CalendarEventAdmin(admin.ModelAdmin):
    list_display = ("title", "event_type", "start_at", "status", "source_system", "is_manual")
    list_filter = ("event_type", "status", "is_manual", "source_system")
    search_fields = ("title", "source_object_id")
    inlines = (CalendarEventAlertInline,)


@admin.register(CalendarSyncEvent)
class CalendarSyncEventAdmin(admin.ModelAdmin):
    list_display = ("source_system", "source_object_id", "action", "created_at")
    list_filter = ("source_system", "action")


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ("title", "category", "priority", "status", "source", "created_by", "created_at")
    list_filter = ("category", "priority", "status", "source")
    search_fields = ("title", "source_service", "source_object_id")
    readonly_fields = ("dedup_hash", "idempotency_key", "recipient_count", "sent_at")
    inlines = (AlertDeliveryInline, AlertRecipientInline)
