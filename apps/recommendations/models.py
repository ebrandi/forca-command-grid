"""Recommendations & Alerts: explainable suggestions and the action queue."""
from __future__ import annotations

from django.conf import settings
from django.db import models

from core.mixins import TimeStampedModel


class Recommendation(TimeStampedModel):
    class Type(models.TextChoices):
        DOCTRINE_READINESS = "doctrine_readiness", "Doctrine readiness"
        SKILL_TRAINING = "skill_training", "Skill training"
        STOCK_SHORTAGE = "stock_shortage", "Stock shortage"
        BUILD_VS_BUY = "build_vs_buy", "Build vs buy"
        MARKET_SEEDING = "market_seeding", "Market seeding"
        HAULING = "hauling", "Hauling"
        NEWBRO_NEXT_STEP = "newbro_next_step", "Newbro next step"
        COMBAT_LOSS_PATTERN = "combat_loss_pattern", "Combat loss pattern"
        OFFICER_ACTION = "officer_action", "Officer action"

    class Confidence(models.TextChoices):
        HIGH = "high", "High"
        MEDIUM = "medium", "Medium"
        LOW = "low", "Low"

    class State(models.TextChoices):
        NEW = "new", "New"
        ACKNOWLEDGED = "acknowledged", "Acknowledged"
        ACTIONED = "actioned", "Actioned"
        DISMISSED = "dismissed", "Dismissed"
        SUPERSEDED = "superseded", "Superseded"

    type = models.CharField(max_length=32, choices=Type.choices, db_index=True)
    subject_type = models.CharField(max_length=32, blank=True)
    subject_id = models.CharField(max_length=64, blank=True)
    message = models.TextField()
    inputs = models.JSONField(default=dict, blank=True)
    logic_summary = models.CharField(max_length=400, blank=True)
    confidence = models.CharField(max_length=8, choices=Confidence.choices, default=Confidence.MEDIUM)
    data_freshness = models.DateTimeField(null=True, blank=True)
    required_permission = models.CharField(max_length=32, default="officer")
    suggested_action = models.JSONField(default=dict, blank=True)
    severity = models.IntegerField(default=0)
    # Estimated ISK at stake (savings, shortfall value, …) for composite ranking;
    # 0 when an evaluator has no natural ISK figure. See services.composite_score.
    isk_impact = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    state = models.CharField(max_length=12, choices=State.choices, default=State.NEW, db_index=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    superseded_by = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="supersedes"
    )

    class Meta:
        ordering = ["-severity", "-created_at"]
        indexes = [models.Index(fields=["type", "subject_type", "subject_id", "state"])]

    def __str__(self) -> str:
        return f"{self.type}:{self.subject_id}"


class Alert(TimeStampedModel):
    class Channel(models.TextChoices):
        IN_APP = "in_app", "In-app"

    recommendation = models.ForeignKey(
        Recommendation, on_delete=models.CASCADE, null=True, blank=True, related_name="alerts"
    )
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    severity = models.IntegerField(default=0)
    channel = models.CharField(max_length=10, choices=Channel.choices, default=Channel.IN_APP)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)


class ActionQueueItem(TimeStampedModel):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In progress"
        DONE = "done", "Done"
        DISMISSED = "dismissed", "Dismissed"

    recommendation = models.ForeignKey(
        Recommendation, on_delete=models.CASCADE, related_name="action_items"
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    linked_project_id = models.BigIntegerField(null=True, blank=True)
    linked_hauling_task_id = models.BigIntegerField(null=True, blank=True)
    notes = models.TextField(blank=True)


class CorpNotification(TimeStampedModel):
    """A relayed ESI in-game notification of interest (structure attack, war, sov…).

    Pulled from a Director's character token and de-duplicated by the ESI
    notification id, so the corp sees defensive alerts on-site and (fresh ones) in
    Discord without logging into every alt.
    """

    notification_id = models.BigIntegerField(primary_key=True)
    type = models.CharField(max_length=64, db_index=True)
    sender_id = models.BigIntegerField(null=True, blank=True)
    sender_type = models.CharField(max_length=32, blank=True)
    timestamp = models.DateTimeField(db_index=True)
    text = models.TextField(blank=True)
    seen = models.BooleanField(default=False)

    class Meta:
        ordering = ["-timestamp"]


class RelayedMail(TimeStampedModel):
    """A corp/alliance mailing-list mail relayed to Discord.

    De-duplicated by the ESI mail id so each announcement is posted once. We keep
    only the header (subject + sender) — never the body — so nothing private is
    stored or relayed.
    """

    mail_id = models.BigIntegerField(primary_key=True)
    subject = models.CharField(max_length=255, blank=True)
    from_id = models.BigIntegerField(null=True, blank=True)
    from_name = models.CharField(max_length=200, blank=True)
    sent_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["-sent_at"]

    def __str__(self) -> str:
        return self.subject or f"Mail {self.mail_id}"


class RecommendationConfig(TimeStampedModel):
    """REC-2 (2.13): leadership-tunable knobs for the recommendation engine — which
    evaluators run, the combat-loss window/threshold, and a severity floor to mute
    low-signal noise. Singleton (via ``active()``), seeded by migration."""

    is_active = models.BooleanField(default=True)
    disabled_evaluators = models.JSONField(
        default=list, blank=True, help_text="Evaluator keys to skip (see the tuning console)."
    )
    combat_loss_window_days = models.PositiveSmallIntegerField(default=7)
    combat_loss_threshold = models.PositiveSmallIntegerField(default=3)
    min_severity = models.PositiveSmallIntegerField(
        default=0, help_text="Drop drafts scoring below this severity (0 = keep all)."
    )

    def __str__(self) -> str:
        return f"RecommendationConfig(min_sev={self.min_severity})"

    @classmethod
    def active(cls) -> RecommendationConfig:
        cfg = cls.objects.filter(is_active=True).order_by("-updated_at").first()
        return cfg or cls.objects.create(is_active=True)
