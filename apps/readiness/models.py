"""Corporation readiness snapshots + findings/recommendations (PRD Module K).

A periodic/on-demand record of the composite readiness index and its dimension
scores, so trends and sparklines are cheap and auditable. Scores only — never
raw member PII. From Phase 2 this app also owns *findings* (detected gaps/risks,
deduped and aged) and the per-pilot output models (created here, populated in a
later phase). It still never owns source data — providers read that from the
existing apps.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import JSONField, Value
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.mixins import TimeStampedModel

from .messages import render_text


class ReadinessSnapshot(TimeStampedModel):
    index = models.IntegerField(default=0)
    dimensions = models.JSONField(default=dict, blank=True)
    coverage = models.JSONField(default=dict, blank=True)
    # Phase-0 additive columns (migration 0002): the per-KPI breakdown, the
    # dimension weights in force, and the config version that produced this
    # snapshot. All default empty/zero so existing rows stay valid and Phase 0
    # writes them inertly (no config layer yet) — the schema is ready for Phase 1.
    kpis = models.JSONField(default=dict, blank=True)
    weights = models.JSONField(default=dict, blank=True)
    config_version = models.IntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"readiness {self.index} @ {self.created_at:%Y-%m-%d %H:%M}"


class ReadinessFinding(models.Model):
    """A detected gap/risk/forecast emitted by a dimension provider during a run.

    Deduped by ``(dimension_key, kpi_key, ref_type, ref_id)`` and updated in place
    (``first_seen``/``last_seen``) so the risk register shows *current* issues with
    age. A finding describes what is wrong; the linked ``tasks.Task`` (via ``task``)
    is the work to fix it. Scores/references only — never raw PII.
    """

    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        ACKNOWLEDGED = "acknowledged", _("Acknowledged")
        RESOLVED = "resolved", _("Resolved")

    class Severity(models.TextChoices):
        INFO = "info", _("Info")
        WARN = "warn", _("Warning")
        HIGH = "high", _("High")
        CRITICAL = "critical", _("Critical")

    class Kind(models.TextChoices):
        GAP = "gap", _("Gap")
        RISK = "risk", _("Risk")
        FORECAST = "forecast", _("Forecast")

    dimension_key = models.CharField(max_length=40, db_index=True)
    kpi_key = models.CharField(max_length=40, blank=True)
    # 0–100 score of the KPI this finding represents (null when it maps to no single
    # KPI); enables score-precise alert matching (score_below / score_above).
    score = models.IntegerField(null=True, blank=True)
    severity = models.CharField(max_length=8, choices=Severity.choices, default=Severity.WARN)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.GAP)
    # The prose columns stay: they are the English fallback AND the audit record of what was
    # actually notified, and they are what legacy rows (written before the *_key columns landed)
    # render from. Nothing is backfilled — a keyless row degrades to its stored English, never
    # to blank.
    title = models.CharField(max_length=200)
    detail = models.TextField(blank=True)
    # Seam B (see ``messages.py``): the writer is a Celery beat with no reader and no locale, so
    # the prose above can only ever be frozen English. These carry the scaffold key + its plain
    # JSON params so ``*_i18n`` can re-render the sentence under the READER's locale.
    title_key = models.CharField(max_length=60, blank=True, default="", db_default="")
    title_params = models.JSONField(blank=True, default=dict, db_default=Value({}, JSONField()))
    detail_key = models.CharField(max_length=60, blank=True, default="", db_default="")
    detail_params = models.JSONField(blank=True, default=dict, db_default=Value({}, JSONField()))
    weight = models.FloatField(default=0.0)
    owner_tag = models.CharField(max_length=40, blank=True)
    task_type = models.CharField(max_length=12, blank=True)
    task_title = models.CharField(max_length=200, blank=True)
    task_title_key = models.CharField(max_length=60, blank=True, default="", db_default="")
    task_title_params = models.JSONField(
        blank=True, default=dict, db_default=Value({}, JSONField())
    )
    ref_type = models.CharField(max_length=40, blank=True)
    ref_id = models.CharField(max_length=64, blank=True)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    predicted_breach_at = models.DateTimeField(null=True, blank=True)
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)
    task = models.ForeignKey(
        "tasks.Task", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["-weight", "-last_seen"]
        constraints = [
            models.UniqueConstraint(
                fields=["dimension_key", "kpi_key", "ref_type", "ref_id"],
                name="uniq_readiness_finding_key",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "dimension_key"]),
            # KPI drill-down filters findings by kpi_key, which the unique constraint
            # (leading column dimension_key) does not serve (M3).
            models.Index(fields=["kpi_key"]),
        ]

    def __str__(self) -> str:
        return f"{self.dimension_key}:{self.title} ({self.status})"

    # --- Seam B read side: resolve under the READER's locale, never the writer's ----------
    # Each falls back to the stored English prose when the row carries no key (every legacy row)
    # or the key is unknown to this deploy. They can never return blank.
    @property
    def title_i18n(self) -> str:
        return render_text(self.title_key, self.title_params, self.title)

    @property
    def detail_i18n(self) -> str:
        return render_text(self.detail_key, self.detail_params, self.detail)

    @property
    def task_title_i18n(self) -> str:
        return render_text(self.task_title_key, self.task_title_params, self.task_title)

    @property
    def age_days(self) -> int:
        return (timezone.now() - self.first_seen).days

    @property
    def is_active(self) -> bool:
        return self.status != self.Status.RESOLVED


class MandatoryShip(models.Model):
    """A ship leadership expects every pilot (or every pilot in a role) to own.

    A leadership-managed list (doc 03 §3.1) the strategic/asset providers read. Either
    a specific hull (``ship_type_id``) or a doctrine fit (``doctrine_fit``)."""

    class Category(models.TextChoices):
        TRAVEL = "travel", _("Travel")
        DOCTRINE = "doctrine", _("Doctrine")
        HOME_DEFENSE = "home_defense", _("Home defense")
        CYNO = "cyno", _("Cyno")
        SCOUT = "scout", _("Scout")
        OTHER = "other", _("Other")

    class LocationKind(models.TextChoices):
        ANY = "any", _("Anywhere")
        SYSTEM = "system", _("Specific system")
        STRUCTURE = "structure", _("Specific structure")

    label = models.CharField(max_length=120)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.OTHER)
    ship_type_id = models.BigIntegerField(null=True, blank=True)
    doctrine_fit = models.ForeignKey(
        "doctrines.DoctrineFit", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    required_quantity = models.PositiveIntegerField(default=1)
    required_location_kind = models.CharField(
        max_length=10, choices=LocationKind.choices, default=LocationKind.ANY
    )
    required_system_id = models.BigIntegerField(null=True, blank=True)
    required_structure_id = models.BigIntegerField(null=True, blank=True)
    require_fitted = models.BooleanField(default=False)
    required_clone = models.BooleanField(default=False)
    required_implants = models.JSONField(default=list, blank=True)
    applies_to_role = models.CharField(max_length=20, blank=True)
    active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "label"]

    def __str__(self) -> str:
        return self.label


class StrategicRoleTarget(models.Model):
    """Desired headcount for a strategic role (doc 03 §3.2), scored by the
    strategic/leadership/fleet_comp providers against qualifying pilots."""

    class Detection(models.TextChoices):
        SKILLS = "skills", _("By skills")
        ASSET = "asset", _("By asset")
        MANUAL = "manual", _("Manual")

    role_key = models.CharField(max_length=20, unique=True)
    label = models.CharField(max_length=80)
    desired_count = models.PositiveIntegerField(default=0)
    detection = models.CharField(max_length=12, choices=Detection.choices, default=Detection.MANUAL)
    detection_params = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["role_key"]

    def __str__(self) -> str:
        return f"{self.label} ×{self.desired_count}"


class PilotReadinessSnapshot(models.Model):
    """Per-pilot score history (one row per character per run). Scores only.

    Created in Phase 2 so the schema lands ahead of the Phase-4 Pilot Dashboard
    that populates it; visible to that pilot and to leadership only.
    """

    # character_id's standalone index is replaced by the (character_id, created_at) composite
    # below — it serves both the per-pilot lookup and the ordered trend slice without a sort (M2).
    character_id = models.BigIntegerField()
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    overall = models.IntegerField(default=0)
    facets = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["character_id", "created_at"])]

    def __str__(self) -> str:
        return f"pilot {self.character_id} readiness {self.overall}"


class ReadinessAlert(models.Model):
    """A fired alert — the audit trail of what was notified, and the dedupe/escalation
    state. An *open* alert (no ``resolved_at``) suppresses re-delivery of the same
    ``rule_key`` within its cooldown; it escalates at most once and resolves at most
    once. Channels actually delivered are recorded for the log."""

    rule_key = models.CharField(max_length=60, db_index=True)
    dimension_key = models.CharField(max_length=40, blank=True)
    kpi_key = models.CharField(max_length=40, blank=True)
    severity = models.CharField(max_length=8, blank=True)
    summary = models.CharField(max_length=300)
    # Seam B: the summary is a copy of the finding's title, frozen at fire time by the
    # ``readiness.alerts`` beat (no reader, no locale). Carry the finding's scaffold key +
    # params across so the alert log renders in the READER's language.
    summary_key = models.CharField(max_length=60, blank=True, default="", db_default="")
    summary_params = models.JSONField(
        blank=True, default=dict, db_default=Value({}, JSONField())
    )
    channels = models.JSONField(default=list, blank=True)
    finding = models.ForeignKey(
        ReadinessFinding, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    escalated_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["rule_key", "resolved_at"])]

    def __str__(self) -> str:
        return f"{self.rule_key} ({self.severity}) @ {self.created_at:%Y-%m-%d %H:%M}"

    @property
    def summary_i18n(self) -> str:
        """The summary under the READER's locale; the stored English when there is no key."""
        return render_text(self.summary_key, self.summary_params, self.summary)

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None


class ExecutiveReport(models.Model):
    """A weekly snapshot of the executive summary, archived + emailable. Keyed on the
    period so a re-run for the same week updates in place (idempotent)."""

    period_start = models.DateField(db_index=True)
    period_end = models.DateField()
    index = models.IntegerField(default=0)
    body = models.JSONField(default=dict, blank=True)
    delivered_channels = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period_start"]
        constraints = [
            models.UniqueConstraint(fields=["period_start", "period_end"], name="uniq_exec_report_period"),
        ]

    def __str__(self) -> str:
        return f"Executive report {self.period_start}–{self.period_end} (index {self.index})"


class PilotRecommendation(models.Model):
    """A pilot's quest-log item. Regenerated each run; user state (done/dismissed/
    snoozed) is preserved across regenerations by the ``(user, category, ref_type,
    ref_id)`` upsert key. Populated by the Phase-4 pilot pipeline."""

    class State(models.TextChoices):
        OPEN = "open", _("Open")
        DONE = "done", _("Done")
        DISMISSED = "dismissed", _("Dismissed")

    class Category(models.TextChoices):
        SHIP = "ship", _("Ship")
        SKILL = "skill", _("Skill")
        ASSET = "asset", _("Asset")
        ROLE = "role", _("Role")
        INDUSTRY = "industry", _("Industry")
        LOGISTICS = "logistics", _("Logistics")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="readiness_recommendations"
    )
    character_id = models.BigIntegerField(null=True, blank=True)
    category = models.CharField(max_length=12, choices=Category.choices)
    title = models.CharField(max_length=200)
    detail = models.TextField(blank=True)
    priority = models.IntegerField(default=0, db_index=True)
    points = models.IntegerField(default=0)
    action_url = models.CharField(max_length=300, blank=True)
    ref_type = models.CharField(max_length=40, blank=True)
    ref_id = models.CharField(max_length=64, blank=True)
    state = models.CharField(
        max_length=12, choices=State.choices, default=State.OPEN, db_index=True
    )
    snoozed_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-priority", "-created_at"]
        constraints = [
            # Scoped to the PILOT, not just the account (LP-3). The row already carried
            # ``character_id`` — it simply was not part of the identity, so regenerating the
            # quest log for one pilot upserted over another pilot's rows and the survivor was
            # shown to whoever asked. Adding it to the key is what makes each pilot's quest log
            # its own.
            models.UniqueConstraint(
                fields=["user", "character_id", "category", "ref_type", "ref_id"],
                name="uniq_pilot_reco_pilot_key",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.category}:{self.title} ({self.state})"


class FleetSupportSkill(models.Model):
    """A skill leadership counts toward fleet-support depth (Gap B4 — Fleet Support).

    A leadership-curated list of booster / warfare-link / logi-support skills, each
    with a desired level. The Fleet Support dimension scores the share of corp members
    trained to each skill's level (mean across the active skills). With **no active
    rows the dimension is unavailable** — it stays disabled until leadership populates
    this list on the Fleet support skills admin page.
    """

    skill_type_id = models.IntegerField(db_index=True)
    # Cached skill name (from the SDE at save time) so the admin list and drill-down
    # render without a per-row type lookup.
    skill_name = models.CharField(max_length=120, blank=True)
    min_level = models.PositiveSmallIntegerField(default=5)
    active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "skill_name"]
        constraints = [
            models.UniqueConstraint(fields=["skill_type_id"], name="uniq_fleet_support_skill"),
        ]

    def __str__(self) -> str:
        return f"{self.skill_name or self.skill_type_id} L{self.min_level}"


class StagingSystem(models.Model):
    """The corp's staging solar system (Gap B5 — Asset Staging).

    A single active row holds the staging system; the Asset Staging dimension scores
    the share of member-owned doctrine hulls sitting in that system (resolved via the
    personal-asset mirror's ``AssetLocation.system_id``). With **no active row the
    dimension is unavailable** — disabled until leadership sets a staging system.
    """

    system_id = models.IntegerField(db_index=True)
    system_name = models.CharField(max_length=120, blank=True)
    active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return self.system_name or f"System {self.system_id}"


class DoctrineReadinessConfig(models.Model):
    """Leadership's readiness classification of a doctrine (design doc 07 §3.3).

    Optional, one row per doctrine. The Doctrine dimension scores exactly as before
    when no rows exist; a row lets leadership mark a doctrine *mandatory* (under-crewing
    it escalates to a high-severity finding) or *retiring* (a past retirement date raises
    a replace-it finding). ``is_primary``/``is_alliance``/``min_pilots`` are recorded for
    drill-down context and future per-KPI splits.
    """

    doctrine = models.OneToOneField(
        "doctrines.Doctrine", on_delete=models.CASCADE, related_name="readiness_config"
    )
    is_primary = models.BooleanField(default=False)
    is_mandatory = models.BooleanField(default=False)
    is_alliance = models.BooleanField(default=False)
    # A doctrine the corp is transitioning TO — drives doctrine.upcoming_coverage (Gap B).
    is_upcoming = models.BooleanField(default=False)
    retirement_date = models.DateField(null=True, blank=True)
    min_pilots = models.PositiveIntegerField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"DoctrineReadinessConfig<{self.doctrine_id}>"
