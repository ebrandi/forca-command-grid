"""Capsuleer Path domain models (design brief §4, doc 07).

``apps.capsuleer`` is the personalised career-planning bounded context: a pilot's profile,
their goals, the milestones and action steps under each goal, explainable suggestions, progress
snapshots and the per-goal activity stream, plus the catalogue of career templates goals start
from. It owns the career domain but integrates with the rest of the platform through *soft
links* only (ADR-0006): ``doctrine_id`` / ``ship_type_id`` / ``skill_plan_id`` / ``task_id`` are
bare ``BigIntegerField`` ids, never cross-app foreign keys, so nothing else in the platform gains
an FK into capsuleer and a deleted doctrine/ship/plan/task can never cascade into a pilot's plan
(the render layer resolves the id defensively).

Only two hard FK shapes exist: composition FKs *inside* the app (a milestone/step/snapshot/
activity row is meaningless without its goal → ``CASCADE``), and account/actor FKs to
``AUTH_USER_MODEL`` — planning data is owned by the account (``CareerProfile`` / ``CareerGoal`` /
``PathSuggestion`` cascade with it, private data must not outlive its owner), while template
authorship and activity actors ``SET_NULL`` so a departed pilot never deletes corp content or
history. The evidence subject is one specific ``sso.EveCharacter`` per goal (``SET_NULL``: an
officer detach survives the goal, which then reads ``unknown`` evidence — the identity rule of
doc 07 §2).

Every model carries ``created_at`` / ``updated_at`` from :class:`core.mixins.TimeStampedModel`.
Stateful invariants (the goal lifecycle transition table, milestone crediting and the endorsement
model, progress recompute, visibility) live in ``services.py`` — the DB enforces only the cheap,
stateless guarantees (named unique + check constraints). Per-kind ``params`` validation lives in
``params.py``; the built-in catalogue and its ``structure`` schema live in ``templates_builtin.py``.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from core.mixins import TimeStampedModel

from .taxonomy import Activity

# ISK money scale shared with campaigns/SRP (doc 07 §1).
_MONEY = {"max_digits": 20, "decimal_places": 2}


# --------------------------------------------------------------------------- #
#  Enumerations (doc 07 §5)
# --------------------------------------------------------------------------- #
class Pace(models.TextChoices):
    RELAXED = "relaxed", _("Relaxed")
    BALANCED = "balanced", _("Balanced")
    ACCELERATED = "accelerated", _("Accelerated")


class GoalPace(models.TextChoices):
    INHERIT = "inherit", _("Inherit from profile")
    RELAXED = "relaxed", _("Relaxed")
    BALANCED = "balanced", _("Balanced")
    ACCELERATED = "accelerated", _("Accelerated")


class CorpAlignment(models.TextChoices):
    PERSONAL_ONLY = "personal_only", _("Personal only")
    MOSTLY_PERSONAL = "mostly_personal", _("Mostly personal")
    BALANCED = "balanced", _("Balanced")
    CORP_FORWARD = "corp_forward", _("Corp forward")
    SHOW_ALL = "show_all", _("Show all")


class Visibility(models.TextChoices):
    PRIVATE = "private", _("Private")
    MENTOR = "mentor", _("Mentor")
    OFFICERS = "officers", _("Officers")
    AGGREGATE_ONLY = "aggregate_only", _("Aggregate only")


class GoalType(models.TextChoices):
    TEMPLATE = "template", _("Template")
    DOCTRINE = "doctrine", _("Doctrine")
    SHIP = "ship", _("Ship")
    ACTIVITY = "activity", _("Activity")
    CUSTOM = "custom", _("Custom")


class GoalStatus(models.TextChoices):
    CONSIDERING = "considering", _("Considering")
    ACTIVE = "active", _("Active")
    PAUSED = "paused", _("Paused")
    COMPLETED = "completed", _("Completed")
    ABANDONED = "abandoned", _("Abandoned")
    ARCHIVED = "archived", _("Archived")


class Priority(models.TextChoices):
    PRIMARY = "primary", pgettext_lazy("goal priority", "Primary")
    SECONDARY = "secondary", _("Secondary")
    SOMEDAY = "someday", _("Someday")


class MilestoneKind(models.TextChoices):
    SKILL_TARGET = "skill_target", _("Skill target")
    DOCTRINE_READY = "doctrine_ready", _("Doctrine ready")
    SHIP_OWNED = "ship_owned", _("Ship owned")
    CONTRIBUTION = "contribution", _("Contribution")
    COMBAT_FIRST = "combat_first", _("Combat first")
    PRACTICAL = "practical", _("Practical")
    MANUAL = "manual", _("Manual")


class MilestoneStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    DONE = "done", _("Done")
    SKIPPED = "skipped", _("Skipped")


class Verification(models.TextChoices):
    AUTO = "auto", _("Automatic")
    SELF = "self", _("Self")
    MENTOR = "mentor", _("Mentor")
    OFFICER = "officer", _("Officer")


class CheckState(models.TextChoices):
    OK = "ok", _("OK")
    UNKNOWN = "unknown", _("Unknown")
    STALE = "stale", _("Stale")


class StepStatus(models.TextChoices):
    OPEN = "open", _("Open")
    DONE = "done", _("Done")
    DISMISSED = "dismissed", _("Dismissed")


class StepSource(models.TextChoices):
    PILOT = "pilot", _("Pilot")
    TEMPLATE = "template", _("Template")
    SUGGESTION = "suggestion", _("Suggestion")


class SuggestionKind(models.TextChoices):
    NEAR_QUALIFICATION = "near_qualification", _("Near qualification")
    EVENT_MATCH = "event_match", _("Event match")
    MENTOR_AVAILABLE = "mentor_available", _("Mentor available")
    STALLED_GOAL = "stalled_goal", _("Stalled goal")
    BLOCKED_PREREQ = "blocked_prereq", _("Blocked prerequisite")
    SHIP_AVAILABLE = "ship_available", _("Ship available")
    CAMPAIGN_OPPORTUNITY = "campaign_opportunity", _("Campaign opportunity")
    REVIEW_DUE = "review_due", _("Review due")


class SuggestionStatus(models.TextChoices):
    OPEN = "open", _("Open")
    ACCEPTED = "accepted", _("Accepted")
    DISMISSED = "dismissed", _("Dismissed")
    DEFERRED = "deferred", _("Deferred")
    NOT_INTERESTED = "not_interested", _("Not interested")
    INCORRECT = "incorrect", _("Incorrect")


class TemplateSource(models.TextChoices):
    BUILTIN = "builtin", _("Built-in")
    CORP = "corp", _("Corp")


class SoloGroup(models.TextChoices):
    SOLO = "solo", _("Solo")
    GROUP = "group", _("Group")
    MIXED = "mixed", _("Mixed")


# --------------------------------------------------------------------------- #
#  Models (doc 07 §4)
# --------------------------------------------------------------------------- #
class CareerProfile(TimeStampedModel):
    """Pilot-controlled planning preferences — one row per account (doc 07 §4.1).

    Owner-only in its entirety (doc 09 §1.2): no field is ever rendered to a mentor, officer or
    aggregate surface. Every field is optional; an empty profile is fully functional. Cascades
    with the account — private planning data must not survive its owner — and is registered in
    ``apps.identity.services.delete_user_data`` (no auto-discovery, doc 07 §11).
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="career_profile"
    )
    preferred_activities = models.JSONField(default=list, blank=True)
    curious_activities = models.JSONField(default=list, blank=True)
    avoided_activities = models.JSONField(default=list, blank=True)
    weekly_hours = models.PositiveSmallIntegerField(null=True, blank=True)
    play_windows = models.CharField(max_length=200, blank=True, default="")
    pace = models.CharField(max_length=12, choices=Pace.choices, default=Pace.BALANCED)
    # Wallet-adjacent: owner-only at field level, every tier, every export, masked under
    # impersonation (doc 09 §1.2). Never aggregated, never logged.
    monthly_budget_isk = models.DecimalField(**_MONEY, null=True, blank=True)
    corp_alignment = models.CharField(
        max_length=16, choices=CorpAlignment.choices, default=CorpAlignment.BALANCED
    )
    mentor_interest = models.BooleanField(default=False)
    default_visibility = models.CharField(
        max_length=16, choices=Visibility.choices, default=Visibility.PRIVATE
    )
    suggestion_muted_kinds = models.JSONField(default=list, blank=True)
    last_reviewed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"CareerProfile<{self.user_id}>"


class CareerTemplate(TimeStampedModel):
    """A catalogue entry for a career path (doc 07 §4.2).

    ``structure`` is a versioned, structure-only blueprint (schema in ``templates_builtin`` /
    doc 07 §7): no people, dates or live values. Built-ins (``source=builtin``, ``created_by``
    null) are seeded idempotently by ``key`` and are edit-locked in the console; corp templates are
    authored by officers. ``key`` is a *named* unique constraint so the seed migration can upsert
    against a stable constraint name.
    """

    key = models.SlugField(max_length=64)
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=32, choices=Activity.choices)
    description = models.TextField(blank=True)
    difficulty = models.PositiveSmallIntegerField(default=1)
    est_hours_note = models.CharField(max_length=120, blank=True, default="")
    cost_note = models.CharField(max_length=120, blank=True, default="")
    solo_group = models.CharField(max_length=8, choices=SoloGroup.choices, default=SoloGroup.MIXED)
    risk_note = models.CharField(max_length=200, blank=True, default="")
    income_note = models.CharField(max_length=200, blank=True, default="")
    newbro_friendly = models.BooleanField(default=False)
    structure = models.JSONField(default=dict)
    source = models.CharField(
        max_length=8, choices=TemplateSource.choices, default=TemplateSource.CORP
    )
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    # Soft link → doctrines.Doctrine; resolved live at instantiation, degrades honestly when
    # dangling (brief §12). Never a cross-app FK.
    doctrine_id = models.BigIntegerField(null=True, blank=True)
    advanced_from = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="advanced_paths"
    )

    class Meta:
        ordering = ["name", "id"]
        indexes = [
            models.Index(fields=["category", "is_active"]),
            models.Index(fields=["source", "is_active"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["key"], name="uniq_cp_template_key"),
            models.CheckConstraint(
                condition=models.Q(difficulty__gte=1) & models.Q(difficulty__lte=3),
                name="ck_cp_template_difficulty",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class CareerGoal(TimeStampedModel):
    """One pilot ambition (doc 07 §4.3).

    Owned by the account (``user``, CASCADE); evidence is read against one ``character`` that must
    belong to the owner (validated server-side, re-checked on every reconcile — doc 07 §2). Status
    changes only through ``services.set_goal_status`` (the transition table is not a DB concern —
    a trigger cannot see the acting user); ``progress_percent`` is recomputed only in the service
    under the goal row lock. ``motivation`` and ``budget_isk`` are owner-only at field level (N-class,
    doc 09) — never rendered to mentor/officer tiers, never in aggregates, logs or notifications.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="career_goals"
    )
    character = models.ForeignKey(
        "sso.EveCharacter", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="career_goals",
    )
    title = models.CharField(max_length=140)
    motivation = models.TextField(blank=True)
    goal_type = models.CharField(max_length=12, choices=GoalType.choices)
    template = models.ForeignKey(
        CareerTemplate, on_delete=models.SET_NULL, null=True, blank=True, related_name="goals"
    )
    template_key = models.SlugField(max_length=64, blank=True, default="")
    doctrine_id = models.BigIntegerField(null=True, blank=True)
    ship_type_id = models.BigIntegerField(null=True, blank=True)
    activity = models.CharField(max_length=32, blank=True, default="")
    status = models.CharField(
        max_length=12, choices=GoalStatus.choices, default=GoalStatus.CONSIDERING
    )
    priority = models.CharField(
        max_length=10, choices=Priority.choices, default=Priority.SECONDARY
    )
    pace = models.CharField(max_length=12, choices=GoalPace.choices, default=GoalPace.INHERIT)
    target_date = models.DateField(null=True, blank=True)
    budget_isk = models.DecimalField(**_MONEY, null=True, blank=True)
    visibility = models.CharField(
        max_length=16, choices=Visibility.choices, default=Visibility.PRIVATE
    )
    corp_alignment_optin = models.BooleanField(default=False)
    progress_percent = models.PositiveSmallIntegerField(default=0)
    # Soft link → skills.SkillPlan written by build_plan (Stage 2); several Goal.CUSTOM plans can
    # exist per character, so the goal holds the back-reference (ADR-0006 shape).
    skill_plan_id = models.BigIntegerField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    paused_reason = models.CharField(max_length=200, blank=True, default="")
    review_due_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["status", "visibility"]),
            models.Index(fields=["status", "review_due_at"]),
        ]
        constraints = [
            # One live goal per (user, template_key) where a template is set: prevents duplicate
            # instantiations while still allowing a completed/abandoned path to be restarted.
            models.UniqueConstraint(
                fields=["user", "template_key"],
                condition=models.Q(template__isnull=False)
                & models.Q(status__in=["considering", "active", "paused"]),
                name="uniq_cp_goal_user_template_active",
            ),
            models.CheckConstraint(
                condition=models.Q(progress_percent__lte=100), name="ck_cp_goal_progress"
            ),
        ]

    def __str__(self) -> str:
        return self.title


class CareerMilestone(TimeStampedModel):
    """An ordered checkpoint within a goal (doc 07 §4.4).

    Inherits the goal's visibility tier. ``params`` follows a per-kind schema (``params.py`` /
    doc 07 §6), validated at authoring time. Crediting is one-way (``pending → done``) except an
    owner un-credit with a recorded reason; mentor/officer-verified milestones are credited by the
    owner only, after a matching endorsement note (doc 09 §3). ``check_state`` is the honest
    tri-state, ``unknown`` until a checker (Stage 2) first runs.
    """

    goal = models.ForeignKey(CareerGoal, on_delete=models.CASCADE, related_name="milestones")
    order = models.PositiveSmallIntegerField()
    title = models.CharField(max_length=140)
    kind = models.CharField(max_length=16, choices=MilestoneKind.choices)
    required = models.BooleanField(default=True)
    params = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=8, choices=MilestoneStatus.choices, default=MilestoneStatus.PENDING
    )
    verification = models.CharField(
        max_length=8, choices=Verification.choices, default=Verification.AUTO
    )
    evidence_note = models.TextField(blank=True)
    # Denormalised at credit time (ADR-0007): upstream stores prune/replace history, so the credit
    # carries its own proof. The verification engine writes source/ref-ids/counts/timestamps only,
    # never priced personal-asset detail or another pilot's identifiers (doc 09 §1.4).
    evidence_snapshot = models.JSONField(default=dict, blank=True)
    due_date = models.DateField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    check_state = models.CharField(
        max_length=8, choices=CheckState.choices, default=CheckState.UNKNOWN
    )
    data_source = models.CharField(max_length=120, blank=True, default="")
    # Stamped by the verification engine when a required checker reports a permanent structural
    # blocker (dangling doctrine, unresolved placeholder, detached character — doc 11 §2). Read on
    # the request path so ``derive_blocked`` never re-runs the engine per goal_detail GET.
    structural_block = models.BooleanField(default=False)

    class Meta:
        ordering = ["order", "id"]
        indexes = [
            models.Index(fields=["status", "verification"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["goal", "order"], name="uniq_cp_milestone_order"),
        ]

    def __str__(self) -> str:
        return self.title


class CareerActionStep(TimeStampedModel):
    """A small practical next action attached to a goal, optionally to a milestone (doc 07 §4.5).

    Pure planning objects, private with the goal. ``task_id`` is set *only* by the explicit
    "make this a corp task" action (Stage 3); a linked corp task is surfaced by its neutral title.
    """

    goal = models.ForeignKey(CareerGoal, on_delete=models.CASCADE, related_name="action_steps")
    milestone = models.ForeignKey(
        CareerMilestone, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="action_steps",
    )
    title = models.CharField(max_length=140)
    note = models.CharField(max_length=300, blank=True, default="")
    status = models.CharField(
        max_length=10, choices=StepStatus.choices, default=StepStatus.OPEN
    )
    source = models.CharField(
        max_length=12, choices=StepSource.choices, default=StepSource.PILOT
    )
    est_cost_isk = models.DecimalField(**_MONEY, null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    # Soft link → tasks.Task, set only by the explicit "create corp task" action (contract C4).
    task_id = models.BigIntegerField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    # The quest-queue "snooze" verb hides a step's quest row until this instant (doc 10 §5.12).
    snoozed_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["goal", "status"]),
        ]

    def __str__(self) -> str:
        return self.title


class PathSuggestion(TimeStampedModel):
    """An explainable, pilot-scoped recommendation (doc 07 §4.6, ADR-0004).

    Deliberately *not* ``apps.recommendations`` (officer-only, no user FK). Owner-only in its
    entirety (doc 09 §1.6): never visible to mentors, officers or aggregates — not its existence,
    status or ``reason``. ``dedupe_key`` is the upsert idempotency key (grammar doc 07 §8);
    ``reason`` is the mandatory "why" (non-blank enforced in ``suggest.py``, Stage 3).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="path_suggestions"
    )
    goal = models.ForeignKey(
        CareerGoal, on_delete=models.SET_NULL, null=True, blank=True, related_name="suggestions"
    )
    kind = models.CharField(max_length=24, choices=SuggestionKind.choices)
    title = models.CharField(max_length=140)
    reason = models.TextField()
    data = models.JSONField(default=dict, blank=True)
    corp_driven = models.BooleanField(default=False)
    status = models.CharField(
        max_length=16, choices=SuggestionStatus.choices, default=SuggestionStatus.OPEN
    )
    dedupe_key = models.CharField(max_length=180)
    expires_at = models.DateTimeField(null=True, blank=True)
    acted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["status", "expires_at"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["dedupe_key"], name="uniq_cp_suggestion_dedupe"),
        ]

    def __str__(self) -> str:
        return f"{self.kind}:{self.user_id}"

    @property
    def data_used_line(self) -> str:
        """A human 'based on:' line from ``data.as_of`` for the suggestion row (AC19, doc 10 §5.9) —
        resolved source labels with relative ages, never raw JSON. Empty when no as-of is recorded."""
        from django.utils.dateparse import parse_datetime
        from django.utils.timesince import timesince

        as_of = (self.data or {}).get("as_of") or {}
        labels = {
            "skills": "your skills", "operations": "upcoming ops", "mentorship": "mentors",
            "assets": "your assets", "prices": "shipyard prices", "campaigns": "campaigns",
            "capsuleer": "your plan",
        }
        parts = []
        for key, label in labels.items():
            if key not in as_of:
                continue
            raw = as_of[key]
            stamp = raw.get("generated_at") if isinstance(raw, dict) else raw
            age = ""
            if isinstance(stamp, str):
                dt = parse_datetime(stamp)
                if dt is not None:
                    age = f" ({timesince(dt)} ago)"
            parts.append(f"{label}{age}")
        return "based on: " + " · ".join(parts) if parts else ""


class ProgressSnapshot(TimeStampedModel):
    """A per-goal history point (doc 07 §4.7).

    Inherits the goal's tier (S-class): powers the goal-detail history chart. ``sp_remaining`` is
    null-for-unknown (never 0-as-unknown); ``notes`` records assumption stamps and must never embed
    budget values. Written by the reconcile paths on change and at most daily (Stage 2), retention-
    capped by the housekeeping beat.
    """

    goal = models.ForeignKey(CareerGoal, on_delete=models.CASCADE, related_name="snapshots")
    taken_at = models.DateTimeField(default=timezone.now)
    percent = models.PositiveSmallIntegerField()
    milestones_done = models.PositiveSmallIntegerField()
    milestones_total = models.PositiveSmallIntegerField()
    sp_remaining = models.BigIntegerField(null=True, blank=True)
    notes = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-taken_at", "-id"]
        indexes = [
            models.Index(fields=["goal", "taken_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(percent__lte=100), name="ck_cp_snapshot_percent"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.goal_id}: {self.percent}%"


class GoalActivity(TimeStampedModel):
    """Append-only per-goal event stream (doc 07 §4.8).

    Inherits the goal's tier (S-class): shown to the owner and shared viewers, and the mentor's
    only write surface (the ``mentor_note`` verb, doc 09 §3). ``detail`` JSON is written exclusively
    by capsuleer services and is tier-safe by construction — verbs, pks, status names, short note
    text — never budget, motivation, paused_reason or suggestion contents. Sensitive officer/mentor
    *reads* additionally hit ``core.audit`` (doc 09 §3.4), which this stream does not replace.
    Append-only: services expose no update/delete path; retention prunes archived-goal rows only.
    """

    # related_name is ``activity_log`` (not ``activity``): CareerGoal already carries an
    # ``activity`` taxonomy field, so the doc 07 §4.8 reverse name is disambiguated here.
    goal = models.ForeignKey(CareerGoal, on_delete=models.CASCADE, related_name="activity_log")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    verb = models.CharField(max_length=64)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["goal", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.goal_id}: {self.verb}"
