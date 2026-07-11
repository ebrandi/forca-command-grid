"""Pingboard — the canonical alerting data model.

Phase 0 lands the core alerting tables (provider registry, templates, alerts,
per-channel deliveries, per-recipient rows). Calendar + automation tables arrive
in their own phases. See ``handbooks/administrator-handbook/console-overview.md``.

Secrets: ``ChannelProvider`` stores per-destination secrets (a Discord webhook
URL, a per-channel token) Fernet-encrypted at rest via ``core.esi.tokens`` behind
a ``@property`` over a ``_secret`` column — the same pattern ``apps.sso.AuthToken``
uses for OAuth tokens. Global provider API tokens (Slack/Telegram/WhatsApp bots)
live in env, not here.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.mixins import TimeStampedModel

log = logging.getLogger("forca.pingboard")


# --- shared vocabulary -------------------------------------------------------
class AlertCategory(models.TextChoices):
    EMERGENCY = "emergency", "Emergency"
    HOME_DEFENCE = "home_defence", "Home defence"
    MINING = "mining", "Mining"
    MOON_EXTRACTION = "moon_extraction", "Moon extraction"
    PVP_FLEET = "pvp_fleet", "PvP fleet"
    ROAMING_GANG = "roaming_gang", "Roaming gang"
    GATECAMP = "gatecamp", "Gatecamp"
    LOGISTICS = "logistics", "Logistics"
    BUYBACK = "buyback", "Buyback"
    MENTORSHIP = "mentorship", "Mentorship"
    INDUSTRY_JOB = "industry_job", "Industry job"
    STRUCTURE_TIMER = "structure_timer", "Structure timer"
    ANNOUNCEMENT = "announcement", "Corporation announcement"
    SYSTEM = "system", "System notification"
    CAMPAIGN = "campaign", "Campaign"
    CAPSULEER = "capsuleer", "Capsuleer Path"
    CUSTOM = "custom", "Custom"


class AlertPriority(models.TextChoices):
    LOW = "low", "Low"
    NORMAL = "normal", "Normal"
    HIGH = "high", "High"
    URGENT = "urgent", "Urgent"
    EMERGENCY = "emergency", "Emergency"


# Priority ordering for gates (dispatch-authority floor, styling, rate tiers).
PRIORITY_RANK = {
    AlertPriority.LOW: 0,
    AlertPriority.NORMAL: 10,
    AlertPriority.HIGH: 20,
    AlertPriority.URGENT: 30,
    AlertPriority.EMERGENCY: 40,
}


class AlertSource(models.TextChoices):
    MANUAL = "manual", "Manual"
    AUTOMATION = "automation", "Automation rule"
    SCHEDULED = "scheduled", "Scheduled"
    SERVICE = "service", "Service-emitted"


class AlertStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    SCHEDULED = "scheduled", "Scheduled"
    QUEUED = "queued", "Queued"
    SENDING = "sending", "Sending"
    SENT = "sent", "Sent"
    PARTIAL = "partial", "Partial failure"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"
    EXPIRED = "expired", "Expired"


# Terminal states — a swept/scheduled alert in one of these is never re-dispatched.
ALERT_TERMINAL = {
    AlertStatus.SENT,
    AlertStatus.PARTIAL,
    AlertStatus.FAILED,
    AlertStatus.CANCELLED,
    AlertStatus.EXPIRED,
}


class ChannelKind(models.TextChoices):
    IN_APP = "in_app", "In-app"
    EVE_MAIL = "eve_mail", "EVE Mail"
    DISCORD = "discord", "Discord"
    SLACK = "slack", "Slack"
    TELEGRAM = "telegram", "Telegram"
    WHATSAPP = "whatsapp", "WhatsApp"


class DeliveryStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SENDING = "sending", "Sending"
    DELIVERED = "delivered", "Delivered"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Skipped"
    RATE_LIMITED = "rate_limited", "Rate limited"


# --- provider configuration (subsumes recommendations.NotificationChannel) ----
class ChannelProvider(TimeStampedModel):
    """One configured destination for a channel kind.

    ``routing`` holds non-secret addressing (a channel id, group id, sender
    character id). ``secret`` (a Discord webhook URL, per-channel token) is
    encrypted at rest. Capability flags drive the composer's audience UI.
    """

    kind = models.CharField(max_length=16, choices=ChannelKind.choices, db_index=True)
    label = models.CharField(max_length=100)
    enabled = models.BooleanField(default=False)  # ships inert
    is_default = models.BooleanField(default=False)  # default destination for its kind
    is_emergency = models.BooleanField(default=False)  # also receives emergency alerts

    routing = models.JSONField(default=dict, blank=True)

    # Encrypted secret payload — never surfaced in the UI/API/logs/audit.
    _secret = models.TextField(db_column="secret_enc", blank=True, default="")

    supports_direct = models.BooleanField(default=False)
    supports_group = models.BooleanField(default=False)
    supports_channel = models.BooleanField(default=False)

    # Classification ceiling — a broadcast provider may never carry a higher tier
    # (generalises command_intel's _BROADCAST_FORBIDDEN). Blank = corp_internal.
    max_classification = models.CharField(max_length=24, blank=True, default="")

    # Health, written by send/test.
    last_ok_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True, default="")  # REDACTED
    last_error_at = models.DateTimeField(null=True, blank=True)
    last_test_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["kind", "label"]
        indexes = [models.Index(fields=["kind", "enabled"])]

    def __str__(self) -> str:
        return f"{self.get_kind_display()}: {self.label}"

    # -- secret accessors (Fernet at rest; degrade to "" on any failure) --------
    @property
    def secret(self) -> str:
        if not self._secret:
            return ""
        from core.esi.tokens import decrypt

        try:
            return decrypt(self._secret)
        except Exception:  # noqa: BLE001 - a bad/rotated key must not crash a send
            log.warning("ChannelProvider %s: secret decrypt failed", self.pk)
            return ""

    @secret.setter
    def secret(self, value: str) -> None:
        from core.esi.tokens import encrypt

        self._secret = encrypt(value or "")

    @property
    def has_secret(self) -> bool:
        return bool(self._secret)


# --- templates ---------------------------------------------------------------
class AlertTemplate(TimeStampedModel):
    key = models.SlugField(max_length=60, unique=True)
    label = models.CharField(max_length=120)
    category = models.CharField(max_length=20, choices=AlertCategory.choices, blank=True, default="")
    subject = models.CharField(max_length=200, blank=True, default="")
    body = models.TextField()
    default_channels = models.JSONField(default=list, blank=True)
    default_priority = models.CharField(
        max_length=12, choices=AlertPriority.choices, default=AlertPriority.NORMAL
    )
    required_vars = models.JSONField(default=list, blank=True)
    is_official = models.BooleanField(default=False)
    enabled = models.BooleanField(default=True)

    class Meta:
        ordering = ["label"]

    def __str__(self) -> str:
        return self.label


# --- automation rules --------------------------------------------------------
class AutomationRule(TimeStampedModel):
    """A director-configured trigger → alert rule. Ships disabled.

    A ``trigger_source`` (e.g. ``srp.submitted``, ``structure.fuel_low``) fires the rule;
    ``condition`` filters it; the rule then emits an alert via the configured template /
    audience / channels, honouring cooldown, per-window caps, expiry and dry-run.
    """

    key = models.SlugField(max_length=60, unique=True)
    label = models.CharField(max_length=120)
    enabled = models.BooleanField(default=False)  # ships inert
    trigger_source = models.CharField(max_length=60, db_index=True)
    condition = models.JSONField(default=dict, blank=True)

    category = models.CharField(max_length=20, choices=AlertCategory.choices)
    template = models.ForeignKey(
        AlertTemplate, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    title = models.CharField(max_length=200, blank=True, default="")
    body = models.TextField(blank=True, default="")
    audience = models.JSONField(default=dict, blank=True)
    channels = models.JSONField(default=list, blank=True)
    priority = models.CharField(
        max_length=12, choices=AlertPriority.choices, default=AlertPriority.NORMAL
    )

    cooldown_minutes = models.IntegerField(default=0)
    max_per_window = models.IntegerField(default=0)  # 0 = unlimited
    window_minutes = models.IntegerField(default=60)
    expires_at = models.DateTimeField(null=True, blank=True)
    dry_run = models.BooleanField(default=False)
    last_fired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["label"]
        indexes = [models.Index(fields=["trigger_source", "enabled"])]

    def __str__(self) -> str:
        return f"{self.label} ({self.trigger_source})"


# --- the central alert -------------------------------------------------------
class Alert(TimeStampedModel):
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True, default="")  # rendered, audit-safe (no secrets)
    category = models.CharField(max_length=20, choices=AlertCategory.choices, db_index=True)
    priority = models.CharField(
        max_length=12, choices=AlertPriority.choices, default=AlertPriority.NORMAL
    )
    severity = models.IntegerField(default=0)  # 0–100
    source = models.CharField(max_length=12, choices=AlertSource.choices, default=AlertSource.MANUAL)
    status = models.CharField(
        max_length=12, choices=AlertStatus.choices, default=AlertStatus.DRAFT, db_index=True
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    scheduled_at = models.DateTimeField(null=True, blank=True, db_index=True)  # None = send now
    expires_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    audience = models.JSONField(default=dict)  # {"kind":"corp"} | {"kind":"users","ids":[...]} | ...
    channels = models.JSONField(default=list)  # ["discord","eve_mail",...]

    reason = models.TextField(blank=True, default="")  # mandatory for urgent/emergency
    confirmation = models.JSONField(default=dict, blank=True)  # {by, at, two_step}

    template = models.ForeignKey(
        AlertTemplate, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    custom_message = models.BooleanField(default=False)
    automation_rule = models.ForeignKey(
        "AutomationRule", on_delete=models.SET_NULL, null=True, blank=True, related_name="alerts"
    )
    source_service = models.CharField(max_length=40, blank=True, default="")
    source_object_id = models.CharField(max_length=64, blank=True, default="")
    calendar_event = models.ForeignKey(
        "CalendarEvent", on_delete=models.SET_NULL, null=True, blank=True, related_name="alerts"
    )

    idempotency_key = models.CharField(max_length=80, blank=True, default="", db_index=True)
    dedup_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    recipient_count = models.IntegerField(default=0)
    retry_count = models.IntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "scheduled_at"]),
            models.Index(fields=["category", "priority", "created_at"]),
            models.Index(fields=["source_service", "source_object_id"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["idempotency_key"],
                condition=Q(idempotency_key__gt=""),
                name="pb_alert_idem",
            )
        ]

    def __str__(self) -> str:
        return f"[{self.priority}] {self.title}"

    @property
    def is_urgent(self) -> bool:
        return PRIORITY_RANK.get(self.priority, 0) >= PRIORITY_RANK[AlertPriority.URGENT]


class AlertDelivery(TimeStampedModel):
    """One row per (alert, channel) — the deliver-once ledger + retry unit."""

    alert = models.ForeignKey(Alert, on_delete=models.CASCADE, related_name="deliveries")
    provider = models.ForeignKey(
        ChannelProvider, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    kind = models.CharField(max_length=16, choices=ChannelKind.choices)
    status = models.CharField(
        max_length=12, choices=DeliveryStatus.choices, default=DeliveryStatus.PENDING, db_index=True
    )
    attempts = models.IntegerField(default=0)
    max_attempts = models.IntegerField(default=4)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    provider_message_id = models.CharField(max_length=128, blank=True, default="")
    last_error = models.CharField(max_length=300, blank=True, default="")  # REDACTED
    recipients_ok = models.IntegerField(default=0)
    recipients_failed = models.IntegerField(default=0)

    class Meta:
        ordering = ["kind"]
        constraints = [
            models.UniqueConstraint(
                fields=["alert", "kind", "provider"], name="pb_delivery_once"
            )
        ]

    def __str__(self) -> str:
        return f"{self.alert_id}:{self.kind}={self.status}"


class AlertRecipient(TimeStampedModel):
    """Per-recipient status where the provider supports it (else a summary row)."""

    alert = models.ForeignKey(Alert, on_delete=models.CASCADE, related_name="recipients")
    kind = models.CharField(max_length=16, choices=ChannelKind.choices)
    recipient_type = models.CharField(max_length=24)  # user|character|discord_user|phone|chat
    recipient_ref = models.CharField(max_length=128)  # NEVER a secret; PII kept out of audit
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    status = models.CharField(
        max_length=12, choices=DeliveryStatus.choices, default=DeliveryStatus.PENDING
    )
    error = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["kind"]
        indexes = [models.Index(fields=["alert", "kind"])]


# --- per-pilot identity linking (opt-in DM handles) --------------------------
class PilotContactChannel(TimeStampedModel):
    """A pilot's opt-in handle for per-channel DMs (Slack / Telegram / WhatsApp / Discord).

    Unverified until the pilot proves ownership (a Telegram deep-link ``/start <code>``,
    or a code delivered to the handle). Only *verified* channels receive DMs; an absent
    or unverified channel is recorded ``SKIPPED``. PII (phone number, chat id) lives ONLY
    here and is never written into ``AuditLog.metadata`` or logs.
    """

    DM_KIND_CHOICES = [
        (ChannelKind.SLACK, "Slack"),
        (ChannelKind.TELEGRAM, "Telegram"),
        (ChannelKind.WHATSAPP, "WhatsApp"),
        (ChannelKind.DISCORD, "Discord"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="pingboard_channels"
    )
    kind = models.CharField(max_length=16, choices=DM_KIND_CHOICES)
    handle = models.CharField(max_length=128, blank=True, default="")  # slack uid / tg chat id / phone
    verified = models.BooleanField(default=False)
    verify_code = models.CharField(max_length=32, blank=True, default="", db_index=True)
    # A verify code is short-lived: a leaked/stale code cannot be redeemed forever to
    # bind an attacker's chat id to this pilot. NULL is treated as already-expired
    # (fail closed) so legacy rows require a fresh link.
    verify_code_expires_at = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["kind"]
        constraints = [
            models.UniqueConstraint(fields=["user", "kind"], name="pb_contact_user_kind")
        ]
        indexes = [models.Index(fields=["kind", "handle"])]

    def __str__(self) -> str:
        state = "verified" if self.verified else "pending"
        return f"{self.get_kind_display()} for {self.user_id} ({state})"


# Categories a pilot may tune on their DM channels. EMERGENCY is a deliberate
# omission — it is a hard safety floor that always reaches every linked channel,
# so an emergency ping can never be silenced by a personal mute. SYSTEM is an
# internal plumbing category and never carries member-facing DM traffic.
MUTABLE_ALERT_CATEGORIES = tuple(
    c for c in AlertCategory if c not in (AlertCategory.EMERGENCY, AlertCategory.SYSTEM)
)


class PilotChannelPreference(TimeStampedModel):
    """A pilot's per-category mute for one of their DM channel kinds.

    The absence of a row means the category is *delivered* on that channel — linking
    a DM channel opts a pilot into everything by default, so this table only ever
    records the categories a pilot chose to *suppress*. A ``muted=True`` row keeps,
    e.g., mining/industry in-app-and-EVE-mail only while a pilot still gets
    home-defence and fleet form-ups on Telegram.

    The mute never touches the in-app or EVE-mail legs (a pilot always keeps those),
    and it is ignored for :attr:`AlertCategory.EMERGENCY` regardless of any stored row
    — an emergency always reaches every linked channel.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="pingboard_prefs"
    )
    kind = models.CharField(max_length=16, choices=PilotContactChannel.DM_KIND_CHOICES)
    category = models.CharField(max_length=20, choices=AlertCategory.choices)
    muted = models.BooleanField(default=True)

    class Meta:
        ordering = ["kind", "category"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "kind", "category"], name="pb_pref_user_kind_cat"
            )
        ]
        indexes = [models.Index(fields=["user", "kind"])]

    def __str__(self) -> str:
        return f"{self.get_kind_display()}/{self.category} muted for {self.user_id}"


# --- calendar ----------------------------------------------------------------
class CalendarEventType(models.TextChoices):
    SCHEDULED_ALERT = "scheduled_alert", "Scheduled alert"
    FLEET_OP = "fleet_op", "Fleet operation"
    EMERGENCY_FLEET = "emergency_fleet", "Emergency fleet"
    MINING = "mining", "Mining operation"
    MOON_EXTRACTION = "moon_extraction", "Moon extraction"
    INDUSTRY_JOB = "industry_job", "Industry job"
    STRUCTURE_TIMER = "structure_timer", "Structure timer"
    LOGISTICS = "logistics", "Logistics event"
    BUYBACK = "buyback", "Buyback event"
    MENTORSHIP = "mentorship", "Mentorship event"
    ANNOUNCEMENT = "announcement", "Corporation announcement"
    CUSTOM = "custom", "Custom timer"


class CalendarEventStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    SCHEDULED = "scheduled", "Scheduled"
    ACTIVE = "active", "Active"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"
    FAILED = "failed", "Failed"
    SYNCED = "synced", "Synced"
    SYNC_CONFLICT = "sync_conflict", "Sync conflict"


# Calendar statuses that are open (a reminder may still fire).
CALENDAR_OPEN_STATUSES = {
    CalendarEventStatus.DRAFT, CalendarEventStatus.SCHEDULED,
    CalendarEventStatus.ACTIVE, CalendarEventStatus.SYNCED,
}


class CalendarEvent(TimeStampedModel):
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    event_type = models.CharField(max_length=20, choices=CalendarEventType.choices, db_index=True)
    start_at = models.DateTimeField(db_index=True)  # EVE/UTC
    end_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=CalendarEventStatus.choices,
        default=CalendarEventStatus.SCHEDULED, db_index=True,
    )

    # Provenance — the idempotency key for automated sync ("" = manual entry).
    source_system = models.CharField(max_length=40, blank=True, default="")
    source_object_id = models.CharField(max_length=64, blank=True, default="")
    is_manual = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    last_synced_at = models.DateTimeField(null=True, blank=True)

    # Fields a human edited — automated sync must never clobber these.
    locked_fields = models.JSONField(default=list, blank=True)

    visibility = models.CharField(max_length=16, default="member")  # member/officer/director
    audience = models.JSONField(default=dict, blank=True)
    default_channels = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["start_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["source_system", "source_object_id"],
                condition=Q(source_system__gt=""),
                name="pb_calevent_source",
            )
        ]
        indexes = [
            models.Index(fields=["event_type", "start_at"]),
            models.Index(fields=["status", "start_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_event_type_display()}: {self.title}"


class CalendarEventAlert(TimeStampedModel):
    """A reminder schedule attached to an event; materialises into an Alert when due."""

    event = models.ForeignKey(CalendarEvent, on_delete=models.CASCADE, related_name="alert_schedules")
    offset_minutes_before = models.IntegerField(default=0)
    template = models.ForeignKey(
        AlertTemplate, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    channels = models.JSONField(default=list, blank=True)
    priority = models.CharField(
        max_length=12, choices=AlertPriority.choices, default=AlertPriority.NORMAL
    )
    audience = models.JSONField(default=dict, blank=True)
    alert = models.ForeignKey(
        Alert, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    cancelled = models.BooleanField(default=False)

    class Meta:
        ordering = ["-offset_minutes_before"]
        constraints = [
            models.UniqueConstraint(
                fields=["event", "offset_minutes_before", "template"],
                name="pb_evt_alert_once",
            )
        ]


class CalendarSyncEvent(TimeStampedModel):
    """Audit + conflict record for one automated sync attempt from a source service."""

    source_system = models.CharField(max_length=40, db_index=True)
    source_object_id = models.CharField(max_length=64, db_index=True)
    event = models.ForeignKey(
        CalendarEvent, on_delete=models.SET_NULL, null=True, blank=True, related_name="sync_events"
    )
    action = models.CharField(max_length=16)  # created/updated/cancelled/conflict/failed/noop
    changed_fields = models.JSONField(default=dict, blank=True)  # {field: [old, new]}
    error = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["source_system", "source_object_id"])]
