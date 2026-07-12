"""Campaign Command domain models (design docs 00 §3, 06).

``apps.campaigns`` is a self-contained bounded context: the corp's strategic-coordination
subsystem (deployments, doctrine roll-outs, stockpile drives, defence readiness). It owns the
whole strategic domain — campaigns, their objectives/milestones/workstreams, risks/issues,
dependencies, evidence, activity, recognition, templates, and linked-operation join rows — and
integrates with the rest of the platform through *soft links* only, never cross-app foreign
keys (the ADR-0006 ``related_type``/``related_id`` convention).

Soft-link discipline (why there are no FKs pointing out of, or into, this app):
  * a linked operation is ``CampaignOperation.operation_id`` (a bare ``BigIntegerField``); the
    operations app never learns it is referenced, so deleting an operation cannot cascade into a
    campaign — the render layer resolves the id defensively and shows "removed" for dangling ids;
  * an improvement plan is ``Campaign.intel_campaign_id`` (soft link to ``command_intel.Campaign``);
  * a dependency edge endpoint is ``(from_kind, from_id)`` / ``(to_kind, to_id)`` — a kind string
    plus an id, so one edge table expresses objective→milestone→workstream→campaign→external
    dependencies without a generic graph engine;
  * EVE entities follow the house ``<thing>_id`` + cached ``<thing>_name`` pattern
    (``staging_system_id`` / ``staging_system_name``).

Only two hard FK shapes exist: composition FKs *inside* the app (child rows are meaningless
without their campaign → ``CASCADE``), and ownership/actor FKs to ``AUTH_USER_MODEL``
(``SET_NULL`` so a departed pilot never deletes campaign history — the "no owner" health rule
surfaces the orphan instead).

Every model carries ``created_at``/``updated_at`` from :class:`core.mixins.TimeStampedModel`.
Stateful invariants (lifecycle transitions, progress/health, sensitivity stripping, dependency
acyclicity) live in ``services.py`` — the DB enforces only what is cheap and stateless (named
unique constraints, ``progress_pct``/``spent_isk`` bounds).
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.mixins import TimeStampedModel


class MeasurementSource(models.TextChoices):
    """Provenance of a measured value — shared by ``Objective.measurement_source`` and
    ``ObjectiveSample.source`` so the manual/auto distinction is one enum (doc 06 §4.9)."""

    AUTO = "auto", _("Automatic")
    MANUAL = "manual", _("Manual")


class DependencyKind(models.TextChoices):
    """Endpoint kind for a dependency edge (doc 06 §4.12). ``external`` is valid only as a
    ``to_kind`` and carries ``to_id=0`` with a mandatory note — the escape hatch for a blocker
    that lives outside any campaign entity (a market delivery, an alliance decision)."""

    OBJECTIVE = "objective", _("Objective")
    MILESTONE = "milestone", _("Milestone")
    WORKSTREAM = "workstream", _("Workstream")
    CAMPAIGN = "campaign", _("Campaign")
    EXTERNAL = "external", _("External")


class EvidenceKind(models.TextChoices):
    """What a piece of evidence hangs off (doc 06 §4.16)."""

    CAMPAIGN = "campaign", _("Campaign")
    OBJECTIVE = "objective", _("Objective")
    MILESTONE = "milestone", _("Milestone")


class ActivitySource(models.TextChoices):
    """Whether an activity row came from a human action or automation (doc 06 §4.17).
    Automation rows always carry ``actor=NULL`` so the two are never confused."""

    MANUAL = "manual", _("Manual")
    AUTOMATION = "automation", _("Automation")


class Campaign(TimeStampedModel):
    """A strategic coordination effort with a lifecycle, objectives and a health signal.

    The lifecycle (``Status``) is a 9-state machine enforced entirely in ``services.py`` — the
    DB stores the current state but never validates the transition (a trigger cannot see the
    acting user's role). ``health`` is derived, never user-set. ``progress_pct`` is a cached
    integer recomputed inside the same transaction as any write that can change it.
    """

    class Category(models.TextChoices):
        DOCTRINE_ROLLOUT = "doctrine_rollout", _("Doctrine Rollout")
        DEPLOYMENT = "deployment", _("Deployment")
        RELOCATION = "relocation", _("Relocation")
        DEFENCE_READINESS = "defence_readiness", _("Defence Readiness")
        STOCKPILE = "stockpile", _("Stockpile")
        SRP_RESERVE = "srp_reserve", _("SRP Reserve")
        MEMBERSHIP = "membership", _("Membership")
        TRAINING = "training", _("Training")
        INDUSTRY = "industry", _("Industry")
        LOGISTICS = "logistics", _("Logistics")
        COVERAGE = "coverage", _("Coverage")
        OTHER = "other", _("Other")

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        PROPOSED = "proposed", _("Proposed")
        APPROVED = "approved", _("Approved")
        ACTIVE = "active", _("Active")
        PAUSED = "paused", _("Paused")
        COMPLETED = "completed", _("Completed")
        FAILED = "failed", _("Failed")
        CANCELLED = "cancelled", _("Cancelled")
        ARCHIVED = "archived", _("Archived")

    class Health(models.TextChoices):
        HEALTHY = "healthy", _("Healthy")
        WATCH = "watch", _("Watch")
        AT_RISK = "at_risk", _("At Risk")
        CRITICAL = "critical", _("Critical")
        BLOCKED = "blocked", _("Blocked")
        UNKNOWN = "unknown", _("Unknown")

    class ProgressMode(models.TextChoices):
        WEIGHTED = "weighted", _("Weighted objectives")
        MILESTONES = "milestones", _("Milestones")
        MANUAL = "manual", _("Manual")

    class Visibility(models.TextChoices):
        MEMBERS = "members", _("Members")
        OFFICERS = "officers", _("Officers")
        DIRECTORS = "directors", _("Directors")
        RESTRICTED = "restricted", _("Restricted")

    class RecognitionMode(models.TextChoices):
        NONE = "none", _("None")
        COUNTS = "counts", _("Counts")
        POINTS = "points", _("Points")

    name = models.CharField(max_length=120)
    summary = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    rationale = models.TextField(blank=True)
    desired_outcome = models.TextField(blank=True)
    category = models.CharField(max_length=32, choices=Category.choices, default=Category.OTHER)
    priority = models.IntegerField(default=0)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    health = models.CharField(max_length=16, choices=Health.choices, default=Health.UNKNOWN)
    health_reasons = models.JSONField(default=list, blank=True)
    progress_pct = models.PositiveSmallIntegerField(default=0)
    progress_mode = models.CharField(
        max_length=16, choices=ProgressMode.choices, default=ProgressMode.WEIGHTED
    )
    progress_note = models.TextField(blank=True)
    sponsor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="sponsored_campaigns",
    )
    commander = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="commanded_campaigns",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    start_at = models.DateTimeField(null=True, blank=True)
    target_end_at = models.DateTimeField(null=True, blank=True)
    actual_end_at = models.DateTimeField(null=True, blank=True)
    budget_isk = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    spent_isk = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    staging_system_id = models.BigIntegerField(null=True, blank=True)
    staging_system_name = models.CharField(max_length=100, blank=True)
    tags = models.JSONField(default=list, blank=True)
    success_criteria = models.TextField(blank=True)
    failure_criteria = models.TextField(blank=True)
    # Drafts must never default member-visible; officers is the conservative floor (doc 06 §3.1).
    visibility = models.CharField(
        max_length=16, choices=Visibility.choices, default=Visibility.OFFICERS
    )
    restricted_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="restricted_campaigns"
    )
    recognition_mode = models.CharField(
        max_length=16, choices=RecognitionMode.choices, default=RecognitionMode.NONE
    )
    recognition_public = models.BooleanField(default=False)
    outcome_summary = models.TextField(blank=True)
    lessons_learned = models.TextField(blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    # Soft link to a command_intel improvement plan (ADR-0006); never a cross-app FK.
    intel_campaign_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-priority", "target_end_at", "-created_at"]
        indexes = [
            models.Index(fields=["status", "health"]),
            models.Index(fields=["status", "target_end_at"]),
            models.Index(fields=["visibility"]),
            models.Index(fields=["category", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(progress_pct__lte=100), name="cmp_campaign_progress_le_100"
            ),
            models.CheckConstraint(
                condition=models.Q(spent_isk__gte=0), name="cmp_campaign_spent_gte_0"
            ),
        ]

    def __str__(self) -> str:
        return self.name


class Workstream(TimeStampedModel):
    """A grouping lane inside a campaign, owned by a ``lead`` who can act within it.

    Progress is derived at read time from the lane's objectives (never stored, doc 06 §3.5), so
    a lane cannot silently disagree with the objectives it groups.
    """

    class WorkstreamStatus(models.TextChoices):
        OPEN = "open", _("Open")
        ON_HOLD = "on_hold", _("On Hold")
        DONE = "done", _("Done")

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="workstreams")
    name = models.CharField(max_length=120)
    key = models.SlugField(max_length=64)
    lead = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    description = models.TextField(blank=True)
    status = models.CharField(
        max_length=16, choices=WorkstreamStatus.choices, default=WorkstreamStatus.OPEN
    )
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]
        constraints = [
            models.UniqueConstraint(fields=["campaign", "key"], name="uniq_cmp_workstream_key"),
        ]

    def __str__(self) -> str:
        return self.name


class Objective(TimeStampedModel):
    """A measurable target within a campaign — the unit weighted progress sums over.

    A ``metric_source`` of ``""`` is a *manual* objective (officer-entered ``current_value``); a
    non-empty key is *auto* (the refresh beat measures it in Phase 3). Both feed the identical
    progress formula in ``progress.py``. ``blocked`` is set only by the issue-linkage rule in
    ``services.py`` (never directly), which stores the prior status so resolution restores it.
    """

    class ObjectiveStatus(models.TextChoices):
        PENDING = "pending", _("Pending")
        ACTIVE = "active", _("Active")
        BLOCKED = "blocked", _("Blocked")
        MET = "met", _("Met")
        MISSED = "missed", _("Missed")
        DROPPED = "dropped", _("Dropped")

    class Direction(models.TextChoices):
        GTE = "gte", _("Reach at least")
        LTE = "lte", _("Keep at or below")

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="objectives")
    workstream = models.ForeignKey(
        Workstream, on_delete=models.SET_NULL, null=True, blank=True, related_name="objectives"
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    weight = models.PositiveIntegerField(default=1)
    due_at = models.DateTimeField(null=True, blank=True)
    is_mandatory = models.BooleanField(default=False)
    help_wanted = models.BooleanField(default=False)
    status = models.CharField(
        max_length=16, choices=ObjectiveStatus.choices, default=ObjectiveStatus.PENDING
    )
    block_reason = models.CharField(max_length=300, blank=True)
    metric_source = models.CharField(max_length=64, blank=True)
    metric_params = models.JSONField(default=dict, blank=True)
    baseline_value = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    target_value = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    current_value = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    unit = models.CharField(max_length=16, blank=True)
    direction = models.CharField(max_length=8, choices=Direction.choices, default=Direction.GTE)
    progress_pct = models.PositiveSmallIntegerField(default=0)
    measured_at = models.DateTimeField(null=True, blank=True)
    measurement_source = models.CharField(
        max_length=8, choices=MeasurementSource.choices, default=MeasurementSource.MANUAL
    )
    measurement_paused = models.BooleanField(default=False)
    last_manual_value_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    last_manual_value_at = models.DateTimeField(null=True, blank=True)
    manual_note = models.TextField(blank=True)
    is_sensitive = models.BooleanField(default=False)
    requires_verification = models.BooleanField(default=False)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    sort_order = models.IntegerField(default=0)

    # Soft-link execution: linked tasks carry related_type/related_id (ADR-0006). The
    # additional-task suffix (``{pk}:{n}``) lets one objective spawn several tasks while a
    # signal still maps every task back to this objective by the ``{pk}`` prefix.
    RELATED_TYPE = "campaign_objective"

    class Meta:
        ordering = ["sort_order", "id"]
        indexes = [
            models.Index(fields=["status", "due_at"]),
            models.Index(fields=["campaign", "status"]),
            models.Index(fields=["owner", "status"]),
            # The refresh beat sweeps auto objectives by source + staleness (doc 08 §2.1);
            # this index serves that ``metric_source != "" ORDER BY measured_at`` shape.
            models.Index(fields=["metric_source", "measured_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(progress_pct__lte=100), name="cmp_objective_progress_le_100"
            ),
        ]

    def linked_tasks(self):
        """The tasks.Task rows soft-linked to this objective (``related_type`` filter).

        Matches the primary task (``related_id == "{pk}"``) and any additional tasks
        (``related_id == "{pk}:{n}"``), mirroring ``command_intel.CourseOfAction.linked_tasks``.
        """
        from apps.tasks.models import Task

        return Task.objects.filter(
            related_type=self.RELATED_TYPE
        ).filter(
            models.Q(related_id=str(self.pk)) | models.Q(related_id__startswith=f"{self.pk}:")
        )

    def __str__(self) -> str:
        return self.title


class ObjectiveSample(TimeStampedModel):
    """Append-only measurement history for an objective (sparkline + freshness honesty).

    Only ``services`` create rows and only housekeeping prunes them; there is no update or
    delete path. ``measured_at`` is the measurement instant (an auto source's ``as_of``), not
    the row insert time — the two differ when ESI data lags the beat run.
    """

    objective = models.ForeignKey(Objective, on_delete=models.CASCADE, related_name="samples")
    value = models.DecimalField(max_digits=20, decimal_places=2)
    measured_at = models.DateTimeField()
    source = models.CharField(
        max_length=8, choices=MeasurementSource.choices, default=MeasurementSource.AUTO
    )
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-measured_at", "-id"]
        indexes = [
            models.Index(fields=["objective", "measured_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.objective_id}: {self.value} @ {self.measured_at:%Y-%m-%d %H:%M}"


class Milestone(TimeStampedModel):
    """A dated deliverable within a campaign, reviewed then approved (doc 04 §5).

    ``ready_for_review → done`` is a separation-of-duties gate enforced in ``services.py``: the
    approver must differ from whoever marked it ready unless they are a director.
    """

    class MilestoneStatus(models.TextChoices):
        PENDING = "pending", _("Pending")
        READY_FOR_REVIEW = "ready_for_review", _("Ready for review")
        DONE = "done", _("Done")
        MISSED = "missed", _("Missed")

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="milestones")
    workstream = models.ForeignKey(
        Workstream, on_delete=models.SET_NULL, null=True, blank=True, related_name="milestones"
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    due_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=MilestoneStatus.choices, default=MilestoneStatus.PENDING
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]
        indexes = [
            models.Index(fields=["status", "due_at"]),
            models.Index(fields=["campaign", "due_at"]),
        ]

    def __str__(self) -> str:
        return self.title


class CampaignDependency(TimeStampedModel):
    """One directed edge: ``from`` is blocked-by ``to`` (doc 06 §3.6, doc 04 §8).

    Deliberately one flat edge table, not a generic graph engine. Endpoints are soft
    ``kind + id`` pairs so a single table spans every entity kind; cycle prevention and
    auto-resolution live in ``services.py``. ``external`` targets carry ``to_id=0`` + a note.
    """

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="dependencies")
    from_kind = models.CharField(max_length=16, choices=DependencyKind.choices)
    from_id = models.BigIntegerField()
    to_kind = models.CharField(max_length=16, choices=DependencyKind.choices)
    to_id = models.BigIntegerField(default=0)
    note = models.CharField(max_length=200, blank=True)
    is_resolved = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["to_kind", "to_id"]),
            models.Index(fields=["campaign", "is_resolved"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "from_kind", "from_id", "to_kind", "to_id"],
                name="uniq_cmp_dependency_edge",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.from_kind}:{self.from_id} → {self.to_kind}:{self.to_id}"


class Risk(TimeStampedModel):
    """A future, uncertain threat scored ``severity = probability × impact`` (1–9).

    ``severity`` is recomputed from ``probability``/``impact`` in ``services.save_risk`` on every
    write and never accepted from input (mass-assignment guard) — the stored default matches
    medium × medium = 4.
    """

    class RiskLevel(models.TextChoices):
        LOW = "low", _("Low")
        MEDIUM = "medium", _("Medium")
        HIGH = "high", _("High")

    class RiskStatus(models.TextChoices):
        OPEN = "open", _("Open")
        MITIGATING = "mitigating", _("Mitigating")
        REALISED = "realised", _("Realised")
        RETIRED = "retired", _("Retired")

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="risks")
    workstream = models.ForeignKey(
        Workstream, on_delete=models.SET_NULL, null=True, blank=True, related_name="risks"
    )
    description = models.TextField()
    probability = models.CharField(
        max_length=8, choices=RiskLevel.choices, default=RiskLevel.MEDIUM
    )
    impact = models.CharField(max_length=8, choices=RiskLevel.choices, default=RiskLevel.MEDIUM)
    severity = models.PositiveSmallIntegerField(default=4)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    mitigation = models.TextField(blank=True)
    contingency = models.TextField(blank=True)
    trigger = models.CharField(max_length=200, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=RiskStatus.choices, default=RiskStatus.OPEN)

    class Meta:
        ordering = ["-severity", "id"]
        indexes = [
            models.Index(fields=["campaign", "status", "severity"]),
        ]

    def __str__(self) -> str:
        return f"{self.description[:60]} (sev {self.severity})"


class Issue(TimeStampedModel):
    """A current, real problem (doc 04 §7). An open or escalated issue linked to an objective
    forces that objective ``blocked`` (service-enforced, reversed when the last such issue
    resolves)."""

    class IssueStatus(models.TextChoices):
        OPEN = "open", _("Open")
        ESCALATED = "escalated", _("Escalated")
        RESOLVED = "resolved", _("Resolved")

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="issues")
    objective = models.ForeignKey(
        Objective, on_delete=models.SET_NULL, null=True, blank=True, related_name="issues"
    )
    description = models.TextField()
    effect = models.TextField(blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    raised_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    target_resolution_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=IssueStatus.choices, default=IssueStatus.OPEN)
    escalated_at = models.DateTimeField(null=True, blank=True)
    resolution_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["campaign", "status"]),
        ]

    def __str__(self) -> str:
        return self.description[:60]


class CampaignEvidence(TimeStampedModel):
    """Link/note evidence attached to a campaign, objective or milestone (doc 06 §3.9).

    No file uploads in v1 — the whole unsafe-upload class is designed out. A row with
    ``added_by=NULL`` is automation-written (a measurement note) and is never editable.
    """

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="evidence")
    attached_kind = models.CharField(
        max_length=16, choices=EvidenceKind.choices, default=EvidenceKind.CAMPAIGN
    )
    attached_id = models.BigIntegerField(default=0)
    url = models.URLField(max_length=400, blank=True)
    note = models.CharField(max_length=400, blank=True)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["attached_kind", "attached_id"]),
        ]

    def __str__(self) -> str:
        return self.url or self.note[:60]


class CampaignActivity(TimeStampedModel):
    """Append-only per-campaign activity stream (in-page history; complements the global
    ``admin_audit.AuditLog``).

    Sensitive verbs (approval, completion, budget change, manual metric override, recognition
    adjustment, visibility change, export) ALSO call ``core.audit.audit_log`` so pruning the
    activity feed can never destroy the audit record. Automation rows use ``actor=NULL``.
    """

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="activity")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    verb = models.CharField(max_length=64)
    target_kind = models.CharField(max_length=16, blank=True)
    target_id = models.BigIntegerField(default=0)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    reason = models.CharField(max_length=300, blank=True)
    source = models.CharField(
        max_length=16, choices=ActivitySource.choices, default=ActivitySource.MANUAL
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["campaign", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.campaign_id}: {self.verb}"


class CampaignRecognition(TimeStampedModel):
    """A manual, audited recognition entry (Phase 4; append-only, doc 06 §3.11).

    ``user`` cascades because the row is *about* the pilot (a deleted account's recognition is
    meaningless while the audit trail survives in ``AuditLog``). No unique constraint by design:
    corrections are compensating negative-``points`` entries, never edits. Automated
    participation is derived at read time from ``pilots.ContributionEvent`` — never stored here.
    """

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="recognitions")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="campaign_recognitions"
    )
    category = models.CharField(max_length=64)
    points = models.IntegerField(default=0)
    reason = models.CharField(max_length=300)
    awarded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["campaign", "user"]),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} · {self.category} ({self.points})"


class CampaignTemplate(TimeStampedModel):
    """A reusable campaign blueprint (structure only — no people, dates or values).

    Builtin rows (``is_builtin=True``) are seeded by an idempotent, reversible migration and are
    clone-to-custom only. "Save as template" from a completed campaign strips instance data.
    ``key`` is a named ``UniqueConstraint`` (not ``unique=True``) so the seed migration can
    upsert by a stable constraint name.
    """

    key = models.SlugField(max_length=64)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    category = models.CharField(
        max_length=32, choices=Campaign.Category.choices, default=Campaign.Category.OTHER
    )
    blueprint = models.JSONField(default=dict, blank=True)
    is_builtin = models.BooleanField(default=False)
    created_from = models.ForeignKey(
        Campaign, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["key"], name="uniq_cmp_template_key"),
        ]

    def __str__(self) -> str:
        return self.name


class CampaignOperation(TimeStampedModel):
    """Join row soft-linking a campaign to an ``operations.Operation`` (doc 06 §3.13).

    The operations app stays unaware of campaigns (no FK into it); ``operation_id`` is a bare
    id. Unique per (campaign, operation) so linking the same op twice is a no-op.
    """

    campaign = models.ForeignKey(
        Campaign, on_delete=models.CASCADE, related_name="linked_operations"
    )
    operation_id = models.BigIntegerField()
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "operation_id"], name="uniq_cmp_campaign_operation"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.campaign_id} ↔ op {self.operation_id}"
