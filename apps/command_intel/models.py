"""Command Intelligence domain models (design doc 03).

The vertical slice (P0–P2) ships five models: the immutable ``IntelligenceSnapshot``
and ``IntelligenceReport``, the derived ``OperationalConstraint``, the stateful
``CourseOfAction``, and the immutable ``ActionOutcome`` calibration record. Campaign
planning (``Campaign``/``CampaignMilestone``) is a later phase (doc 18 P4) and is not
modelled here yet — so ``CourseOfAction`` carries no ``campaign`` FK until then
(the roadmap's forward-FK rule).
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.mixins import TimeStampedModel


class Trigger(models.TextChoices):
    MANUAL = "manual", _("Manual")
    SCHEDULED = "scheduled", _("Scheduled")
    OUTCOME = "outcome", _("Outcome measurement")
    AUTONOMOUS = "autonomous", _("Autonomous proposal")


class Severity(models.TextChoices):
    INFO = "info", _("Info")
    WATCH = "watch", _("Watch")
    HIGH = "high", _("High")
    CRITICAL = "critical", _("Critical")


class Classification(models.TextChoices):
    CORP_INTERNAL = "corp_internal", _("Corporation Internal")
    HIGH_COMMAND = "high_command", _("High Command")
    DIRECTOR_EYES_ONLY = "director_eyes_only", _("Director — Eyes Only")
    ALLIANCE_COMMAND = "alliance_command", _("Alliance Command")


class IntelligenceSnapshot(TimeStampedModel):
    """Immutable, point-in-time facts assembled from every source (doc 03 §3.1).

    The single input the LLM ever sees. Stored as JSON blobs (no per-source tables).
    """

    slices = models.JSONField(default=dict, blank=True)          # {source_key: {facts}}
    coverage = models.JSONField(default=dict, blank=True)        # {source_key: {as_of, coverage_pct, status}}
    source_versions = models.JSONField(default=dict, blank=True)
    config_version = models.IntegerField(default=0)
    schema_version = models.IntegerField(default=1)
    trigger = models.CharField(max_length=16, choices=Trigger.choices, default=Trigger.MANUAL)
    build_ms = models.IntegerField(default=0)
    built_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="command_intel_snapshots",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["-created_at"])]

    def __str__(self) -> str:
        return f"Snapshot #{self.pk} @ {self.created_at:%Y-%m-%d %H:%M}"


class OperationalConstraint(TimeStampedModel):
    """A computed limit on maximum capability, derived from a snapshot (doc 03 §3.2)."""

    class Status(models.TextChoices):
        COMPUTED = "computed", _("Computed")
        UNKNOWN = "unknown", _("Unknown (insufficient data)")
        UNAVAILABLE = "unavailable", _("Unavailable")

    snapshot = models.ForeignKey(
        IntelligenceSnapshot, on_delete=models.CASCADE, related_name="constraints"
    )
    key = models.CharField(max_length=80)
    category = models.CharField(max_length=24)
    label = models.CharField(max_length=160)
    binding_metric = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    unit = models.CharField(max_length=24, blank=True)
    limiting_factor = models.CharField(max_length=80, blank=True)
    headroom = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    score = models.IntegerField(null=True, blank=True)
    severity = models.CharField(max_length=12, choices=Severity.choices, default=Severity.INFO)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.COMPUTED)
    affected_capabilities = models.JSONField(default=list, blank=True)
    evidence = models.JSONField(default=list, blank=True)
    detail = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["snapshot", "key"], name="uniq_ci_constraint_key"),
        ]
        indexes = [
            models.Index(fields=["snapshot", "-severity"]),
            models.Index(fields=["key", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.label} ({self.binding_metric} {self.unit})"


class IntelligenceReport(TimeStampedModel):
    """The generated staff briefing — immutable once ``ready`` (doc 03 §3.3)."""

    class Status(models.TextChoices):
        QUEUED = "queued", _("Queued")
        BUILDING_SNAPSHOT = "building_snapshot", _("Building snapshot")
        COMPUTING_CONSTRAINTS = "computing_constraints", _("Computing constraints")
        CALLING_LLM = "calling_llm", _("Calling LLM")
        VALIDATING = "validating", _("Validating")
        READY = "ready", _("Ready")
        READY_DEGRADED = "ready_degraded", _("Ready (degraded — no narrative)")
        FAILED = "failed", _("Failed")

    snapshot = models.ForeignKey(
        IntelligenceSnapshot, on_delete=models.PROTECT, null=True, blank=True,
        related_name="reports",
    )
    template_key = models.CharField(max_length=64, default="posture")
    classification = models.CharField(
        max_length=24, choices=Classification.choices, default=Classification.HIGH_COMMAND
    )
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.QUEUED)
    title = models.CharField(max_length=200, blank=True)
    summary = models.TextField(blank=True)
    body = models.JSONField(default=dict, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="command_intel_reports",
    )
    trigger = models.CharField(max_length=16, choices=Trigger.choices, default=Trigger.MANUAL)
    model_name = models.CharField(max_length=64, blank=True)
    prompt_version = models.IntegerField(default=0)
    config_version = models.IntegerField(default=0)
    token_usage = models.JSONField(default=dict, blank=True)
    latency_ms = models.IntegerField(default=0)
    repair_attempts_used = models.IntegerField(default=0)
    grounding_violations_dropped = models.IntegerField(default=0)
    error = models.TextField(blank=True)
    generated_at = models.DateTimeField(null=True, blank=True)
    # Scheduled-delivery bookkeeping: {"discord": n, "evemail": m}. Drives deliver-once
    # (a report is announced at most once per channel) and is empty for manual reports.
    delivered_channels = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["classification", "-created_at"]),
            models.Index(fields=["status"]),
            models.Index(fields=["trigger", "-created_at"]),
        ]

    @property
    def is_terminal(self) -> bool:
        return self.status in {self.Status.READY, self.Status.READY_DEGRADED, self.Status.FAILED}

    def __str__(self) -> str:
        return self.title or f"Report #{self.pk}"


class CourseOfAction(TimeStampedModel):
    """A structured, owned, prioritised proposed action (doc 03 §3.4)."""

    class Effort(models.TextChoices):
        LOW = "low", _("Low")
        MEDIUM = "medium", _("Medium")
        HIGH = "high", _("High")

    class State(models.TextChoices):
        PROPOSED = "proposed", _("Proposed")
        ACCEPTED = "accepted", _("Accepted")
        IN_PROGRESS = "in_progress", _("In progress")
        COMPLETED = "completed", _("Completed")
        DISMISSED = "dismissed", _("Dismissed")
        SUPERSEDED = "superseded", _("Superseded")

    class ConfidenceLabel(models.TextChoices):
        HIGH = "high", _("High")
        MEDIUM = "medium", _("Medium")
        LOW = "low", _("Low")

    report = models.ForeignKey(
        IntelligenceReport, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="courses_of_action",
    )
    constraint = models.ForeignKey(
        OperationalConstraint, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="courses_of_action",
    )
    slug = models.CharField(max_length=160, db_index=True)
    objective = models.TextField()
    reasoning = models.TextField(blank=True)
    expected_impact = models.JSONField(default=dict, blank=True)
    readiness_delta = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    effort = models.CharField(max_length=8, choices=Effort.choices, default=Effort.MEDIUM)
    priority = models.IntegerField(default=0)
    confidence = models.FloatField(default=0.0)
    confidence_label = models.CharField(
        max_length=8, choices=ConfidenceLabel.choices, default=ConfidenceLabel.MEDIUM
    )
    owner_tag = models.CharField(max_length=48, blank=True)
    responsible_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="command_intel_coas",
    )
    risk_if_ignored = models.TextField(blank=True)
    severity_if_ignored = models.CharField(
        max_length=12, choices=Severity.choices, default=Severity.WATCH
    )
    dependencies = models.ManyToManyField("self", symmetrical=False, blank=True, related_name="dependents")
    provenance = models.JSONField(default=dict, blank=True)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PROPOSED)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="command_intel_coa_decisions",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)
    baseline_snapshot = models.ForeignKey(
        IntelligenceSnapshot, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="baseline_for_coas",
    )
    # Set when grouped into a campaign (added additively in P4 — doc 18 forward-FK rule).
    campaign = models.ForeignKey(
        "Campaign", on_delete=models.SET_NULL, null=True, blank=True, related_name="grouped_coas",
    )

    # The soft-link to execution: tasks carry related_type/related_id (doc 07 §6).
    RELATED_TYPE = "command_intel_coa"

    class Meta:
        ordering = ["-priority", "-created_at"]
        indexes = [
            models.Index(fields=["state", "-priority"]),
            models.Index(fields=["slug"]),
        ]

    @property
    def is_active(self) -> bool:
        return self.state in {self.State.PROPOSED, self.State.ACCEPTED, self.State.IN_PROGRESS}

    def linked_tasks(self):
        """The tasks.Task rows this COA spawned (soft-link, doc 07 §6)."""
        from apps.tasks.models import Task

        return Task.objects.filter(related_type=self.RELATED_TYPE, related_id=str(self.pk))

    def __str__(self) -> str:
        return self.objective[:80]


class ActionOutcome(TimeStampedModel):
    """The predicted-vs-measured calibration record for a completed COA (doc 03 §3.6)."""

    coa = models.ForeignKey(
        CourseOfAction, on_delete=models.CASCADE, related_name="outcomes"
    )
    metric_key = models.CharField(max_length=64)
    predicted_delta = models.DecimalField(max_digits=8, decimal_places=2)
    measured_delta = models.DecimalField(max_digits=8, decimal_places=2)
    error = models.DecimalField(max_digits=8, decimal_places=2)
    baseline_snapshot = models.ForeignKey(
        IntelligenceSnapshot, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="outcome_baseline_for",
    )
    outcome_snapshot = models.ForeignKey(
        IntelligenceSnapshot, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="outcome_result_for",
    )
    measured_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.metric_key}: predicted {self.predicted_delta} / measured {self.measured_delta}"


class Campaign(TimeStampedModel):
    """A sequenced improvement plan toward a target metric (doc 03 §3.5, doc 08)."""

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        ACTIVE = "active", _("Active")
        COMPLETED = "completed", _("Completed")
        ABANDONED = "abandoned", _("Abandoned")

    objective = models.TextField()
    target_metric = models.CharField(max_length=80)            # "readiness.overall" or a constraint key
    baseline_value = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    target_value = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    success_probability = models.FloatField(default=0.0)
    expected_trajectory = models.JSONField(default=list, blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="command_intel_campaigns",
    )
    created_from_report = models.ForeignKey(
        IntelligenceReport, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="campaigns",
    )
    start_at = models.DateTimeField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "-created_at"])]

    @property
    def progress_pct(self) -> int:
        total = self.milestones.count()
        if not total:
            return 0
        done = self.milestones.filter(status=CampaignMilestone.Status.DONE).count()
        return round(100 * done / total)

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE

    def __str__(self) -> str:
        return self.objective[:80]


class CampaignMilestone(TimeStampedModel):
    """One sequenced step of a campaign, optionally wrapping a COA (doc 03 §3.5)."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        IN_PROGRESS = "in_progress", _("In progress")
        DONE = "done", _("Done")
        BLOCKED = "blocked", _("Blocked")

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="milestones")
    order = models.IntegerField(default=0)
    title = models.CharField(max_length=200)
    coa = models.ForeignKey(
        CourseOfAction, on_delete=models.SET_NULL, null=True, blank=True, related_name="milestones",
    )
    expected_value = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    responsible_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="command_intel_milestones",
    )
    owner_tag = models.CharField(max_length=48, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    dependencies = models.ManyToManyField("self", symmetrical=False, blank=True, related_name="dependents")

    class Meta:
        ordering = ["campaign", "order"]

    def __str__(self) -> str:
        return f"{self.order}. {self.title}"


class PilotDirective(TimeStampedModel):
    """One corp-aligned "next best action" for a member, ranked by constraint-relief
    leverage (Pilot Intelligence, design doc 16 §7).

    The CI analogue of the readiness ``PilotRecommendation`` quest log: upserted by
    ``(user, slug)`` so a pilot's done/snooze/dismiss state survives regeneration, and an
    OPEN directive whose underlying corp constraint no longer binds is dropped. Unlike a
    readiness recommendation, a directive's ranking is *CI-aware* — its ``leverage`` is the
    severity of the corp's binding constraint this pilot can personally help relieve.
    """

    class State(models.TextChoices):
        OPEN = "open", _("Open")
        DONE = "done", _("Done")
        DISMISSED = "dismissed", _("Dismissed")

    class Category(models.TextChoices):
        SKILL = "skill", _("Training")
        SHIP = "ship", _("Ship")
        LOGISTICS = "logistics", _("Logistics")
        ROLE = "role", _("Role")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="command_intel_directives",
    )
    slug = models.CharField(max_length=160)
    constraint_key = models.CharField(max_length=80, blank=True)
    category = models.CharField(max_length=16, choices=Category.choices, default=Category.SKILL)
    title = models.CharField(max_length=200)
    detail = models.TextField(blank=True)
    # The ranking score: how binding the corp constraint this directive relieves is
    # (severity-weighted). Higher = more strategic leverage for this pilot's one move.
    leverage = models.IntegerField(default=0)
    points = models.IntegerField(default=0)
    posture_lift = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    action_url = models.CharField(max_length=200, blank=True)
    state = models.CharField(max_length=12, choices=State.choices, default=State.OPEN)
    snoozed_until = models.DateTimeField(null=True, blank=True)
    # CMD-2 (3.6): stable completion timestamp (set once when first marked DONE) — the
    # recognition credit + raffle ticket source key off this, not the churny updated_at.
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-leverage", "-points", "-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "slug"], name="uniq_ci_directive_user_slug"),
        ]
        indexes = [models.Index(fields=["user", "state"])]

    @property
    def is_open(self) -> bool:
        return self.state == self.State.OPEN

    def __str__(self) -> str:
        return f"{self.user_id}: {self.title[:60]}"


class ConversationTurn(TimeStampedModel):
    """One grounded question-and-answer over the intelligence archive (P7, doc 17 §3).

    Read-only conversational intelligence: the retriever gathers the classification-filtered
    archive passages the asker is cleared to see, the LLM answers ONLY from those and cites
    them, and the turn is persisted for audit. Self-only — a turn belongs to its asker. The
    LLM call runs in a worker (ADR-0008), so a turn starts ``pending`` and the UI polls until
    it is terminal. Degrades to a retrieval-only listing when the LLM is unavailable.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        ANSWERING = "answering", _("Answering")
        READY = "ready", _("Ready")
        READY_DEGRADED = "ready_degraded", _("Ready (degraded — retrieval only)")
        FAILED = "failed", _("Failed")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="command_intel_questions",
    )
    question = models.TextField()
    answer = models.TextField(blank=True)
    citations = models.JSONField(default=list, blank=True)   # [{id, kind, title, ref_url}]
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    # The asker's clearance ceiling at ask time — audit of what could have been surfaced.
    clearance = models.CharField(max_length=24, blank=True)
    grounded = models.BooleanField(default=False)
    model_name = models.CharField(max_length=64, blank=True)
    token_usage = models.JSONField(default=dict, blank=True)
    latency_ms = models.IntegerField(default=0)
    error = models.TextField(blank=True)
    answered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["status"]),
        ]

    @property
    def is_terminal(self) -> bool:
        return self.status in {self.Status.READY, self.Status.READY_DEGRADED, self.Status.FAILED}

    def __str__(self) -> str:
        return self.question[:80]


class BattleAnalysis(TimeStampedModel):
    """An AI after-action review of a killboard battle (Combat Intelligence).

    Soft-linked to a ``killboard.BattleReport`` by id (the ADR-0006 convention — no cross-app
    schema dependency). The deterministic ``facts`` are computed by ``battle.battle_facts``;
    the LLM narrative ``body`` only interprets them (what happened / what went wrong / what to
    improve), grounded against the facts. Worker-generated (ADR-0008): starts ``pending`` and
    the UI polls until terminal; degrades to a facts-only body when the LLM is unavailable.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        BUILDING_FACTS = "building_facts", _("Building facts")
        CALLING_LLM = "calling_llm", _("Calling LLM")
        READY = "ready", _("Ready")
        READY_DEGRADED = "ready_degraded", _("Ready (degraded — facts only)")
        FAILED = "failed", _("Failed")

    battle_report_id = models.IntegerField(db_index=True)
    classification = models.CharField(
        max_length=24, choices=Classification.choices, default=Classification.HIGH_COMMAND
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    title = models.CharField(max_length=200, blank=True)
    facts = models.JSONField(default=dict, blank=True)
    body = models.JSONField(default=dict, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="command_intel_battle_analyses",
    )
    model_name = models.CharField(max_length=64, blank=True)
    token_usage = models.JSONField(default=dict, blank=True)
    latency_ms = models.IntegerField(default=0)
    grounding_violations_dropped = models.IntegerField(default=0)
    error = models.TextField(blank=True)
    generated_at = models.DateTimeField(null=True, blank=True)

    # Soft-link to the killboard battle (ADR-0006 convention; no cross-app FK).
    RELATED_TYPE = "killboard_battle"

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["battle_report_id", "-created_at"]),
            models.Index(fields=["status"]),
        ]

    @property
    def is_terminal(self) -> bool:
        return self.status in {self.Status.READY, self.Status.READY_DEGRADED, self.Status.FAILED}

    def __str__(self) -> str:
        return self.title or f"Battle analysis #{self.pk}"


class SavedSimScenario(TimeStampedModel):
    """A named, re-runnable what-if for the readiness simulator (4.18).

    Stores only the *inputs* (which stressor + magnitude), never a frozen result — so a
    saved scenario always re-runs against the LATEST snapshot when loaded or compared,
    making it a live contingency-planning tool rather than a stale screenshot. Corp-wide
    (any officer sees the shared library); ``created_by`` is provenance only."""

    name = models.CharField(max_length=120)
    scenario_key = models.CharField(max_length=40)
    magnitude = models.FloatField(default=0)
    notes = models.CharField(max_length=280, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="saved_sim_scenarios",
    )

    class Meta:
        ordering = ["name", "id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.scenario_key}×{self.magnitude})"
