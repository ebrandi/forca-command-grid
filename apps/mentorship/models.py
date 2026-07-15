"""Mentorship Program data model.

Pairs new pilots (mentees / "cadets") with experienced ones (mentors /
"veterans") and gives them a configurable learning path, honest validation, an
auditable reward ledger and leadership reporting. Design doc:
``docs/design/mentorship-program.md``.

Design notes that shape this module:

* **Planning / record only.** Nothing here ever moves ISK in-game. An ISK reward
  is *recorded* in ``MentorshipRewardLedger`` and an officer marks it paid with a
  free-text reference — exactly like SRP (``apps.srp``). This is deliberate: the
  platform has no safe in-game transfer, so we never pretend otherwise.
* **Config is a leadership-tunable singleton** (``MentorshipProgram``, mirrors
  ``SrpProgram``); tracks, tasks, reward rules and badges are leader-CRUD'd
  catalogues. Per-pair progress lives on ``MentorshipTaskAssignment``.
* **Honest validation.** A task declares *how* it is validated
  (``MentorshipTask.validation_method``) and, for auto checks, *what* to check
  (``MentorshipTask.criteria`` — a JSON rule dispatched by ``validation.py``,
  mirroring onboarding's ``_criterion_met``). Each sign-off / auto-check writes an
  append-only ``MentorshipTaskValidation`` row with a confidence score.
* **Append-only audit trails**: ``MentorshipPairingEvent`` (pairing lifecycle),
  ``MentorshipTaskValidation`` (every validation step), ``MentorshipRewardLedger``
  status stamps, and ``MentorshipFlag`` (anomaly detection). Sensitive admin
  actions additionally go through ``core.audit.audit_log``.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.sso.models import EveCharacter
from core.mixins import TimeStampedModel

from . import messages as msg
from . import tracks_i18n

# Seam B (see ``messages.py``): the prose columns below are written by one actor — usually a Celery
# worker, which has no user and therefore no locale — and read back by *other* people under *their*
# locale. ``gettext`` at the write site cannot help: Django coerces a lazy proxy to ``str`` on
# ``.save()``, freezing the row in the writer's (English) locale forever, and a proxy inside a
# JSONField is a hard TypeError. So each sink keeps its English prose column (the fallback *and* the
# audit record) and gains a ``_key`` + ``_params`` pair that the ``_i18n`` read property re-resolves
# under the reader's locale. Rows written before this change carry no key and degrade to their
# stored English — never to blank.
#
# ``db_default`` (not just ``default``) is load-bearing on every new column: without it Django emits
# ``ADD COLUMN … DEFAULT x NOT NULL`` followed by ``ALTER COLUMN … DROP DEFAULT``, leaving a NOT NULL
# column with no database-level default. Any INSERT from older code during a rollback then fails with
# a not-null violation, while reads keep working — so it looks fine and breaks silently.
_KEY_FIELD_KWARGS = {"max_length": 64, "blank": True, "default": "", "db_default": ""}
# NB: ``Value({}, JSONField())`` — not ``Value("{}", …)``, which Django would encode as the JSON
# *string* ``"{}"`` and hand an old-code INSERT a ``str`` where every reader expects a dict.
_PARAMS_DB_DEFAULT = models.Value({}, models.JSONField())


# ---------------------------------------------------------------------------
# Configuration (leadership-tunable)
# ---------------------------------------------------------------------------
class MentorshipProgram(TimeStampedModel):
    """Leadership-tunable settings for the whole programme (a singleton).

    ``services.active_program()`` returns the live one, seeding a sensible
    default on first use (mirrors ``SrpProgram``). The Admin Console edits it via
    ``MentorshipProgramForm``.
    """

    class EligibilityLogic(models.TextChoices):
        EITHER = "either", _("Meet either threshold")
        BOTH = "both", _("Meet both thresholds")

    class RewardMode(models.TextChoices):
        # Reward is only written to the ledger as owed; no approval workflow.
        RECORDED_ONLY = "recorded_only", _("Record as owed only")
        # Reward is queued for leadership approval before it can be paid.
        QUEUED = "queued", _("Queue for leadership approval")
        # Reward is auto-approved on grant (still recorded, still paid manually).
        AUTO = "auto", _("Auto-approve on grant")

    class ProfileVisibility(models.TextChoices):
        MEMBERS = "members", _("All corp members")
        OFFICERS = "officers", _("Officers only")

    name = models.CharField(max_length=80, default="Mentorship Program")
    is_active = models.BooleanField(default=True)
    # Master switch: when off, pilots see the programme is paused and can't join.
    enabled = models.BooleanField(default=True)
    intro_text = models.TextField(
        blank=True,
        default=(
            "New to the corp or to EVE? Our veterans will fly with you and show you "
            "the ropes — from your first overview setup to your first fleet kill. "
            "Sign up as a cadet, get paired with a mentor, and work through hands-on "
            "field exercises at your own pace."
        ),
    )

    # --- Eligibility (configurable thresholds) ---
    mentor_min_character_age_days = models.PositiveIntegerField(
        default=365, help_text=_("Mentor: minimum character age (from ESI birthday).")
    )
    mentor_min_corp_tenure_days = models.PositiveIntegerField(
        default=182, help_text=_("Mentor: minimum time in the corp (~6 months).")
    )
    mentor_eligibility_logic = models.CharField(
        max_length=8, choices=EligibilityLogic.choices, default=EligibilityLogic.EITHER,
        help_text=_("Whether a mentor must meet either or both age/tenure thresholds."),
    )
    mentee_max_corp_tenure_days = models.PositiveIntegerField(
        default=90, help_text=_("Mentee: must have been in the corp less than this (~3 months).")
    )
    enforce_mentee_eligibility = models.BooleanField(
        default=True,
        help_text=_("If off, any member may register as a mentee regardless of tenure."),
    )

    # --- Approvals ---
    mentor_requires_approval = models.BooleanField(default=True)
    mentee_requires_approval = models.BooleanField(default=False)

    # --- Pairing ---
    max_active_mentees_per_mentor = models.PositiveIntegerField(default=3)
    allow_mentor_initiated = models.BooleanField(
        default=True, help_text=_("Mentors may invite a specific mentee.")
    )
    allow_mentee_initiated = models.BooleanField(
        default=True, help_text=_("Mentees may request a specific mentor.")
    )
    pairing_requires_approval = models.BooleanField(
        default=True, help_text=_("A pairing needs leadership approval before it goes active."),
    )
    pairing_ttl_days = models.PositiveIntegerField(
        default=14, help_text=_("Auto-expire suggested/requested pairings after N days (0 = never).")
    )
    stale_pair_days = models.PositiveIntegerField(
        default=14, help_text=_("Flag an active pair with no activity for N days (0 = never).")
    )

    # --- Rewards ---
    rewards_enabled = models.BooleanField(default=True)
    reward_mode = models.CharField(
        max_length=16, choices=RewardMode.choices, default=RewardMode.QUEUED
    )
    esi_validation_required = models.BooleanField(
        default=False,
        help_text=_("Rewardable tasks must pass an ESI/internal auto-check, not just a sign-off."),
    )
    allow_unverified_rewards = models.BooleanField(
        default=True,
        help_text=_("Allow rewards for tasks completed by sign-off alone (no auto-verification)."),
    )
    default_task_cooldown_hours = models.PositiveIntegerField(
        default=0, help_text=_("Default anti-farming cooldown between repeats of a task (0 = none).")
    )
    mentee_reward_cap_isk = models.DecimalField(
        max_digits=20, decimal_places=2, default=0,
        help_text=_("Max ISK a mentee can accrue in the current cohort/season (0 = no cap)."),
    )
    mentor_reward_cap_isk = models.DecimalField(
        max_digits=20, decimal_places=2, default=0,
        help_text=_("Max ISK a mentor can accrue in the current cohort/season (0 = no cap)."),
    )

    # --- Visibility ---
    mentor_directory_visible = models.BooleanField(
        default=True, help_text=_("Show the mentor directory to eligible mentees.")
    )
    profile_visibility = models.CharField(
        max_length=8, choices=ProfileVisibility.choices, default=ProfileVisibility.MEMBERS,
        help_text=_("Who can see mentor/mentee profiles and progress (beyond the pair itself)."),
    )

    # --- Notifications ---
    notify_discord = models.BooleanField(
        default=False,
        help_text=_("Broadcast programme events (new applications, stalled pairs) to Discord. "
                    "Disarmed by default; needs a configured Discord webhook."),
    )

    # --- Season / cohort ---
    active_cohort = models.ForeignKey(
        "MentorshipCohort", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
        help_text=_("The current intake; new registrations attach to it."),
    )

    class Meta:
        ordering = ["-is_active", "-updated_at"]

    def __str__(self) -> str:
        return f"MentorshipProgram<{self.name}{' active' if self.is_active else ''}>"

    @property
    def intro_text_i18n(self) -> str:
        """``intro_text`` under the reader's locale while it still holds the shipped default."""
        return tracks_i18n.program_intro(self.intro_text)


class MentorshipCohort(TimeStampedModel):
    """A season / intake, e.g. "Q3 2026 Rookie Intake"."""

    key = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    starts_on = models.DateField(null=True, blank=True)
    ends_on = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ["-starts_on", "-created_at"]

    def __str__(self) -> str:
        return self.name


# ---------------------------------------------------------------------------
# Learning tracks & tasks (leader-CRUD catalogue)
# ---------------------------------------------------------------------------
class MentorshipTrack(TimeStampedModel):
    """A group of tasks that teaches one area of EVE / corp life."""

    class Category(models.TextChoices):
        WELCOME = "welcome", _("Welcome to the corporation")
        CLIENT = "client", _("EVE client & overview")
        TRAVEL = "travel", _("Travel, safety & survival")
        FITTING = "fitting", _("Fitting & doctrine basics")
        RATTING = "ratting", _("Ratting")
        MINING = "mining", _("Mining")
        EXPLORATION = "exploration", _("Exploration")
        PVP = "pvp", _("PvP basics")
        FLEET = "fleet", _("Fleet operations")
        LOGISTICS = "logistics", _("Logistics & buyback services")
        INDUSTRY = "industry", _("Manufacturing & industry")
        SKILLS = "skills", _("Skill planning")
        OTHER = "other", _("Other")

    key = models.SlugField(max_length=64, unique=True)
    title = models.CharField(max_length=140)
    summary = models.CharField(max_length=240, blank=True)
    description = models.TextField(blank=True)
    category = models.CharField(
        max_length=16, choices=Category.choices, default=Category.OTHER
    )
    icon = models.CharField(max_length=32, default="i-rookie", help_text=_("Sprite id, e.g. i-ship."))
    is_core = models.BooleanField(
        default=False, help_text=_("Part of the core cadet path (auto-enrolled on pairing).")
    )
    estimated_sessions = models.PositiveIntegerField(default=1)
    sort_order = models.IntegerField(default=0)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "title"]

    def __str__(self) -> str:
        return self.title

    @property
    def title_i18n(self) -> str:
        """``title`` under the reader's locale while it still holds the seeded English."""
        return tracks_i18n.track_title(self.key, self.title)

    @property
    def summary_i18n(self) -> str:
        """``summary`` under the reader's locale while it still holds the seeded English."""
        return tracks_i18n.track_summary(self.key, self.summary)


class MentorshipTask(TimeStampedModel):
    """One field exercise inside a track."""

    class Difficulty(models.TextChoices):
        INTRO = "intro", _("Intro")
        BASIC = "basic", _("Basic")
        INTERMEDIATE = "intermediate", _("Intermediate")
        ADVANCED = "advanced", _("Advanced")

    class Participants(models.TextChoices):
        MENTEE = "mentee", _("Mentee only")
        MENTOR = "mentor", _("Mentor only")
        BOTH = "both", _("Mentor & mentee together")
        FLEET = "fleet", _("A fleet / group")

    class Validation(models.TextChoices):
        MANUAL_MENTOR = "manual_mentor", _("Mentor sign-off")
        MENTEE_CONFIRM = "mentee_confirm", _("Mentee self-confirmation")
        DUAL_CONFIRM = "dual_confirm", _("Both mentor & mentee confirm")
        LEADERSHIP = "leadership", _("Leadership approval")
        API_ASSISTED = "api_assisted", _("API-assisted (auto-check + mentor sign-off)")
        API_REQUIRED = "api_required", _("API-required (auto-check must pass)")
        EVIDENCE = "evidence", _("Evidence link/note + mentor sign-off")
        AUTO_INTERNAL = "auto_internal", _("Auto from Command Grid activity")
        HYBRID = "hybrid", _("Hybrid (auto-check contributes; mentor confirms)")

    class Evidence(models.TextChoices):
        NONE = "none", _("Not required")
        OPTIONAL = "optional", _("Optional")
        REQUIRED = "required", _("Required")

    class EvidenceKind(models.TextChoices):
        LINK = "link", _("A link (killboard, screenshot host, doc)")
        TEXT = "text", _("A written note")

    class Visibility(models.TextChoices):
        PAIR = "pair", _("Mentor & mentee")
        MENTOR_ONLY = "mentor_only", _("Mentor only (prep/debrief)")

    track = models.ForeignKey(
        MentorshipTrack, on_delete=models.CASCADE, related_name="tasks"
    )
    key = models.SlugField(max_length=80, unique=True)
    title = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    difficulty = models.CharField(
        max_length=12, choices=Difficulty.choices, default=Difficulty.BASIC
    )
    estimated_minutes = models.PositiveIntegerField(default=30)
    participants = models.CharField(
        max_length=8, choices=Participants.choices, default=Participants.BOTH
    )
    mentor_instructions = models.TextField(blank=True)
    mentee_instructions = models.TextField(blank=True)

    validation_method = models.CharField(
        max_length=16, choices=Validation.choices, default=Validation.MANUAL_MENTOR
    )
    # Auto-check rule (dispatched by validation.py). e.g.
    # {"type": "skill_min", "skill_type_id": 3300, "level": 3}.
    criteria = models.JSONField(default=dict, blank=True)

    evidence_requirement = models.CharField(
        max_length=8, choices=Evidence.choices, default=Evidence.NONE
    )
    evidence_kind = models.CharField(
        max_length=8, choices=EvidenceKind.choices, default=EvidenceKind.LINK
    )

    reward_eligible = models.BooleanField(default=False)
    cooldown_hours = models.PositiveIntegerField(default=0)
    repeatable = models.BooleanField(default=False)
    max_repeats = models.PositiveIntegerField(default=1)
    mandatory = models.BooleanField(default=False)
    visibility = models.CharField(
        max_length=12, choices=Visibility.choices, default=Visibility.PAIR
    )
    sort_order = models.IntegerField(default=0)
    tags = models.JSONField(default=list, blank=True)
    admin_notes = models.TextField(blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["track", "sort_order", "title"]
        indexes = [models.Index(fields=["track", "active"])]

    def __str__(self) -> str:
        return self.title

    @property
    def is_auto(self) -> bool:
        return self.validation_method in {
            self.Validation.API_ASSISTED, self.Validation.API_REQUIRED,
            self.Validation.AUTO_INTERNAL, self.Validation.HYBRID,
        }

    @property
    def title_i18n(self) -> str:
        """``title`` under the reader's locale while it still holds the seeded English."""
        return tracks_i18n.task_title(self.key, self.title)

    @property
    def mentee_instructions_i18n(self) -> str:
        """``mentee_instructions`` under the reader's locale while unedited."""
        return tracks_i18n.task_mentee_instructions(self.key, self.mentee_instructions)

    @property
    def mentor_instructions_i18n(self) -> str:
        """``mentor_instructions`` under the reader's locale while unedited."""
        return tracks_i18n.task_mentor_instructions(self.key, self.mentor_instructions)


class MentorshipTaskPrerequisite(models.Model):
    """``task`` can't start until ``requires`` is completed (per pairing)."""

    task = models.ForeignKey(
        MentorshipTask, on_delete=models.CASCADE, related_name="prerequisites"
    )
    requires = models.ForeignKey(
        MentorshipTask, on_delete=models.CASCADE, related_name="required_by"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["task", "requires"], name="uniq_mentorship_prereq"),
            models.CheckConstraint(
                condition=~models.Q(task=models.F("requires")), name="mentorship_prereq_not_self"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.task_id} needs {self.requires_id}"


# ---------------------------------------------------------------------------
# Recognition badges (referenced by reward rules)
# ---------------------------------------------------------------------------
class MentorshipBadge(TimeStampedModel):
    """A cosmetic recognition badge (a "certification")."""

    class Tier(models.TextChoices):
        BRONZE = "bronze", _("Bronze")
        SILVER = "silver", _("Silver")
        GOLD = "gold", _("Gold")

    class Audience(models.TextChoices):
        MENTOR = "mentor", _("Mentor")
        MENTEE = "mentee", _("Mentee")
        BOTH = "both", _("Both")

    key = models.SlugField(max_length=64, unique=True)
    label = models.CharField(max_length=100)
    description = models.CharField(max_length=240, blank=True)
    icon = models.CharField(max_length=32, default="i-trophy")
    tier = models.CharField(max_length=8, choices=Tier.choices, default=Tier.BRONZE)
    audience = models.CharField(max_length=8, choices=Audience.choices, default=Audience.BOTH)
    sort_order = models.IntegerField(default=0)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "label"]

    def __str__(self) -> str:
        return self.label

    @property
    def label_i18n(self) -> str:
        """``label`` under the reader's locale while it still holds the seeded English."""
        return tracks_i18n.badge_label(self.key, self.label)


class MentorshipRewardRule(TimeStampedModel):
    """When to grant a reward, to whom, and how much."""

    class Audience(models.TextChoices):
        MENTOR = "mentor", _("Mentor")
        MENTEE = "mentee", _("Mentee")
        BOTH = "both", _("Both")

    class Trigger(models.TextChoices):
        TASK = "task", _("A specific task completes")
        TRACK_COMPLETE = "track_complete", _("A track is completed")
        PROGRAM_COMPLETE = "program_complete", _("The whole programme is completed")
        MILESTONE = "milestone", _("A named milestone")
        PAIRING_ACTIVE_DAYS = "pairing_active_days", _("A pairing stays active N days")
        SESSION = "session", _("A mentorship session is confirmed")

    class RewardType(models.TextChoices):
        ISK = "isk", _("ISK (recorded, paid manually)")
        POINTS = "points", _("Contribution points")
        BADGE = "badge", _("Recognition badge")
        TITLE = "title", _("Title / recognition text")
        CUSTOM = "custom", _("Custom (paid outside the system)")

    key = models.SlugField(max_length=80, unique=True)
    label = models.CharField(max_length=140)
    description = models.CharField(max_length=240, blank=True)
    audience = models.CharField(max_length=8, choices=Audience.choices, default=Audience.MENTEE)
    trigger = models.CharField(max_length=20, choices=Trigger.choices, default=Trigger.TASK)
    # Meaning depends on trigger: task key / track key / milestone key / day count.
    trigger_ref = models.CharField(max_length=80, blank=True)

    reward_type = models.CharField(max_length=8, choices=RewardType.choices, default=RewardType.POINTS)
    amount = models.DecimalField(max_digits=20, decimal_places=2, default=0, help_text=_("ISK amount."))
    points = models.IntegerField(default=0)
    badge = models.ForeignKey(
        MentorshipBadge, on_delete=models.SET_NULL, null=True, blank=True, related_name="reward_rules"
    )
    title_text = models.CharField(max_length=120, blank=True)

    cap_per_recipient = models.DecimalField(
        max_digits=20, decimal_places=2, default=0,
        help_text=_("Max total from this rule per recipient (0 = no cap)."),
    )
    cooldown_hours = models.PositiveIntegerField(default=0)
    requires_leadership_approval = models.BooleanField(default=True)
    requires_verification = models.BooleanField(
        default=False, help_text=_("Only grant if the triggering task was auto-verified."),
    )
    cohort = models.ForeignKey(
        MentorshipCohort, on_delete=models.SET_NULL, null=True, blank=True, related_name="reward_rules"
    )
    active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "label"]
        indexes = [models.Index(fields=["trigger", "active"])]

    def __str__(self) -> str:
        return self.label


# ---------------------------------------------------------------------------
# Participant profiles
# ---------------------------------------------------------------------------
class _ProfileStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    PENDING = "pending", _("Pending approval")
    ACTIVE = "active", _("Active")
    PAUSED = "paused", _("Paused")
    REJECTED = "rejected", _("Rejected")
    RETIRED = "retired", _("Retired")


class MentorProfile(TimeStampedModel):
    """A veteran who has volunteered to mentor."""

    Status = _ProfileStatus

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="mentor_profile"
    )
    character = models.ForeignKey(
        EveCharacter, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True
    )

    # Preferences / matching signals.
    areas = models.JSONField(default=list, blank=True, help_text=_("Track categories + free tags."))
    timezone = models.CharField(max_length=64, blank=True)
    play_windows = models.CharField(max_length=200, blank=True)
    languages = models.JSONField(default=list, blank=True)
    comms = models.CharField(max_length=120, blank=True)
    max_active_mentees = models.PositiveIntegerField(
        default=0, help_text=_("0 = use the programme default.")
    )
    open_to_adhoc = models.BooleanField(default=True)
    bio = models.TextField(blank=True)
    restrictions = models.CharField(max_length=200, blank=True)

    applied_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    reject_reason = models.CharField(max_length=240, blank=True)
    # Eligibility snapshot: {eligible, character_age_days, corp_tenure_days,
    # confidence, reasons, computed_at, source}.
    eligibility = models.JSONField(default=dict, blank=True)
    cohort = models.ForeignKey(
        MentorshipCohort, on_delete=models.SET_NULL, null=True, blank=True, related_name="mentors"
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Mentor<{self.user_id}:{self.status}>"


class MenteeProfile(TimeStampedModel):
    """A new pilot (cadet) seeking mentorship."""

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        PENDING = "pending", _("Pending approval")
        ACTIVE = "active", _("Active")
        PAUSED = "paused", _("Paused")
        GRADUATED = "graduated", _("Graduated")
        REJECTED = "rejected", _("Rejected")

    class Experience(models.TextChoices):
        BRAND_NEW = "brand_new", _("Brand new to EVE")
        RETURNING = "returning", _("Returning after a break")
        SOME_EXP = "some_exp", _("Some experience, new to the corp")

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="mentee_profile"
    )
    character = models.ForeignKey(
        EveCharacter, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True
    )

    goals = models.JSONField(default=list, blank=True, help_text=_("Track categories they want to learn."))
    experience = models.CharField(
        max_length=12, choices=Experience.choices, default=Experience.BRAND_NEW
    )
    timezone = models.CharField(max_length=64, blank=True)
    play_windows = models.CharField(max_length=200, blank=True)
    languages = models.JSONField(default=list, blank=True)
    interests = models.JSONField(default=list, blank=True)
    ships_can_fly = models.CharField(max_length=240, blank=True)
    needs_skill_help = models.BooleanField(default=False)
    needs_fitting_help = models.BooleanField(default=False)
    voice_comfortable = models.BooleanField(default=False)
    notes = models.TextField(blank=True)

    applied_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    reject_reason = models.CharField(max_length=240, blank=True)
    eligibility = models.JSONField(default=dict, blank=True)
    cohort = models.ForeignKey(
        MentorshipCohort, on_delete=models.SET_NULL, null=True, blank=True, related_name="mentees"
    )
    graduated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Mentee<{self.user_id}:{self.status}>"


# ---------------------------------------------------------------------------
# Pairing lifecycle
# ---------------------------------------------------------------------------
class MentorshipPairing(TimeStampedModel):
    """A mentor↔mentee relationship and its lifecycle."""

    class Status(models.TextChoices):
        SUGGESTED = "suggested", _("Suggested")
        REQUESTED = "requested", _("Requested")
        PENDING_APPROVAL = "pending_approval", _("Pending approval")
        ACTIVE = "active", _("Active")
        PAUSED = "paused", _("Paused")
        COMPLETED = "completed", _("Completed")
        CANCELLED = "cancelled", _("Cancelled")
        EXPIRED = "expired", _("Expired")

    class InitiatedBy(models.TextChoices):
        MENTOR = "mentor", _("Mentor")
        MENTEE = "mentee", _("Mentee")
        LEADER = "leader", _("Leadership")
        SYSTEM = "system", _("System (auto-suggested)")

    # Non-terminal states that block a duplicate pairing for the same pair.
    OPEN_STATUSES = frozenset({
        Status.SUGGESTED, Status.REQUESTED, Status.PENDING_APPROVAL, Status.ACTIVE, Status.PAUSED,
    })
    # States that actually consume one of a mentor's mentoring slots. Pending
    # approvals do NOT — a leader may queue many, but only ``capacity`` can be made
    # active, which ``set_status`` enforces at activation time.
    CAPACITY_STATUSES = frozenset({Status.ACTIVE, Status.PAUSED})
    TERMINAL_STATUSES = frozenset({Status.COMPLETED, Status.CANCELLED, Status.EXPIRED})

    mentor = models.ForeignKey(
        MentorProfile, on_delete=models.CASCADE, related_name="pairings"
    )
    mentee = models.ForeignKey(
        MenteeProfile, on_delete=models.CASCADE, related_name="pairings"
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.SUGGESTED, db_index=True
    )
    initiated_by = models.CharField(
        max_length=8, choices=InitiatedBy.choices, default=InitiatedBy.SYSTEM
    )
    match_score = models.FloatField(null=True, blank=True)
    match_reasons = models.JSONField(default=list, blank=True)
    # Seam B: ``[{"key": …, "params": {…}}, …]``, written in lockstep with (and in the same order
    # as) the English ``match_reasons`` by the auto-suggest worker.
    match_reasons_keys = models.JSONField(
        default=list, blank=True, db_default=models.Value([], models.JSONField())
    )
    cohort = models.ForeignKey(
        MentorshipCohort, on_delete=models.SET_NULL, null=True, blank=True, related_name="pairings"
    )
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    last_activity_at = models.DateTimeField(null=True, blank=True, db_index=True)
    note = models.TextField(blank=True)
    pause_reason = models.CharField(max_length=200, blank=True)
    completion_note = models.CharField(max_length=240, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["mentor", "status"]),
            models.Index(fields=["mentee", "status"]),
        ]

    def __str__(self) -> str:
        return f"Pairing<{self.mentor_id}->{self.mentee_id}:{self.status}>"

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE

    @property
    def match_reasons_i18n(self) -> list[str]:
        """``match_reasons`` under the reader's locale; the stored English for legacy rows."""
        return msg.render_list(self.match_reasons_keys, self.match_reasons)

    def touch_activity(self, when=None) -> None:
        self.last_activity_at = when or timezone.now()


class MentorshipPairingEvent(models.Model):
    """Append-only pairing lifecycle / activity log."""

    class Kind(models.TextChoices):
        STATUS = "status", _("Status change")
        NOTE = "note", _("Note")
        SYSTEM = "system", _("System")

    pairing = models.ForeignKey(
        MentorshipPairing, on_delete=models.CASCADE, related_name="events"
    )
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.STATUS)
    from_status = models.CharField(max_length=16, blank=True)
    to_status = models.CharField(max_length=16, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    detail = models.CharField(max_length=300, blank=True)
    # Seam B: the reader-locale form of ``detail``. Empty for a free-text detail (a pause/cancel
    # reason a pilot typed) and for legacy rows — both then render ``detail`` verbatim.
    detail_key = models.CharField(**_KEY_FIELD_KWARGS)
    detail_params = models.JSONField(default=dict, blank=True, db_default=_PARAMS_DB_DEFAULT)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.pairing_id}:{self.kind}:{self.to_status or self.detail[:20]}"

    @property
    def detail_i18n(self) -> str:
        """``detail`` under the *reader's* locale; the stored English when there is no key."""
        return msg.render(self.detail_key, self.detail_params, self.detail)


class MentorshipEnrollment(TimeStampedModel):
    """A pairing working through a track."""

    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        COMPLETED = "completed", _("Completed")
        PAUSED = "paused", _("Paused")

    pairing = models.ForeignKey(
        MentorshipPairing, on_delete=models.CASCADE, related_name="enrollments"
    )
    track = models.ForeignKey(
        MentorshipTrack, on_delete=models.CASCADE, related_name="enrollments"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["pairing", "track"], name="uniq_mentorship_enrollment")
        ]
        ordering = ["track__sort_order"]

    def __str__(self) -> str:
        return f"Enroll<{self.pairing_id}:{self.track_id}:{self.status}>"


# ---------------------------------------------------------------------------
# Task execution & validation
# ---------------------------------------------------------------------------
class MentorshipTaskAssignment(TimeStampedModel):
    """A task's progress for one pairing (the completion state lives here)."""

    class Status(models.TextChoices):
        NOT_STARTED = "not_started", _("Not started")
        IN_PROGRESS = "in_progress", _("In progress")
        SUBMITTED = "submitted", _("Submitted")
        PENDING_MENTOR = "pending_mentor", _("Pending mentor confirmation")
        PENDING_MENTEE = "pending_mentee", _("Pending mentee confirmation")
        PENDING_API = "pending_api", _("Pending API validation")
        PENDING_LEADERSHIP = "pending_leadership", _("Pending leadership approval")
        COMPLETED = "completed", _("Completed")
        COMPLETED_UNREWARDABLE = "completed_unrewardable", _("Completed (not rewardable)")
        REJECTED = "rejected", _("Rejected")
        EXPIRED = "expired", _("Expired")
        WAIVED = "waived", _("Waived")

    DONE_STATUSES = frozenset({Status.COMPLETED, Status.COMPLETED_UNREWARDABLE, Status.WAIVED})
    PENDING_STATUSES = frozenset({
        Status.SUBMITTED, Status.PENDING_MENTOR, Status.PENDING_MENTEE,
        Status.PENDING_API, Status.PENDING_LEADERSHIP,
    })

    pairing = models.ForeignKey(
        MentorshipPairing, on_delete=models.CASCADE, related_name="assignments"
    )
    task = models.ForeignKey(
        MentorshipTask, on_delete=models.CASCADE, related_name="assignments"
    )
    repeat_index = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=24, choices=Status.choices, default=Status.NOT_STARTED, db_index=True
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    due_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # Whether this completion is eligible to trigger a reward (verified enough per
    # the programme's policy). Separate from "completed for learning".
    rewardable = models.BooleanField(default=False)
    # Best validation confidence achieved (0–100).
    confidence = models.PositiveIntegerField(default=0)
    last_reason = models.CharField(max_length=300, blank=True)
    # Seam B: the reader-locale form of ``last_reason``. Empty when the reason is free text a
    # mentor/officer typed (rejections, waivers) — that stays verbatim in every locale.
    last_reason_key = models.CharField(**_KEY_FIELD_KWARGS)
    last_reason_params = models.JSONField(default=dict, blank=True, db_default=_PARAMS_DB_DEFAULT)
    # Anti-farming: earliest time this (repeatable) task can be redone.
    cooldown_until = models.DateTimeField(null=True, blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ["task__track__sort_order", "task__sort_order", "repeat_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["pairing", "task", "repeat_index"], name="uniq_mentorship_assignment"
            )
        ]
        indexes = [
            models.Index(fields=["pairing", "status"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        return f"Assign<{self.pairing_id}:{self.task_id}:{self.status}>"

    @property
    def is_done(self) -> bool:
        return self.status in self.DONE_STATUSES

    @property
    def last_reason_i18n(self) -> str:
        """``last_reason`` under the reader's locale; the stored English when there is no key."""
        return msg.render(self.last_reason_key, self.last_reason_params, self.last_reason)


class MentorshipTaskValidation(models.Model):
    """One validation step for an assignment (append-only audit)."""

    class Source(models.TextChoices):
        MENTOR = "mentor", _("Mentor")
        MENTEE = "mentee", _("Mentee")
        LEADERSHIP = "leadership", _("Leadership")
        API = "api", _("ESI auto-check")
        INTERNAL = "internal", _("Command Grid activity")
        SYSTEM = "system", _("System")

    class Result(models.TextChoices):
        PENDING = "pending", _("Pending")
        PASS = "pass", _("Pass")
        FAIL = "fail", _("Fail")

    assignment = models.ForeignKey(
        MentorshipTaskAssignment, on_delete=models.CASCADE, related_name="validations"
    )
    source = models.CharField(max_length=12, choices=Source.choices)
    result = models.CharField(max_length=8, choices=Result.choices, default=Result.PENDING)
    confidence = models.PositiveIntegerField(default=0)
    detail = models.CharField(max_length=300, blank=True)
    # Seam B: the reader-locale form of ``detail``. Auto-check details are written by the Celery
    # sweep; a mentor's rejection note is free text and stays verbatim (no key).
    detail_key = models.CharField(**_KEY_FIELD_KWARGS)
    detail_params = models.JSONField(default=dict, blank=True, db_default=_PARAMS_DB_DEFAULT)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    evidence = models.ForeignKey(
        "MentorshipEvidence", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        # Officer reporting group-bys scan this append-only log filtering result=FAIL and
        # source=MENTOR/result=PASS; a (result, source) composite serves both (M1).
        indexes = [models.Index(fields=["result", "source"])]

    def __str__(self) -> str:
        return f"Valid<{self.assignment_id}:{self.source}:{self.result}>"

    @property
    def detail_i18n(self) -> str:
        """``detail`` under the reader's locale; the stored English when there is no key."""
        return msg.render(self.detail_key, self.detail_params, self.detail)


class MentorshipEvidence(TimeStampedModel):
    """A link or note submitted as evidence for a task."""

    class Kind(models.TextChoices):
        LINK = "link", _("Link")
        NOTE = "note", _("Note")

    assignment = models.ForeignKey(
        MentorshipTaskAssignment, on_delete=models.CASCADE, related_name="evidence_items"
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.LINK)
    url = models.URLField(blank=True)
    text = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Evidence<{self.assignment_id}:{self.kind}>"


# ---------------------------------------------------------------------------
# Sessions (scheduled mentoring)
# ---------------------------------------------------------------------------
class MentorshipSession(TimeStampedModel):
    """A scheduled (or logged) mentoring session."""

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", _("Scheduled")
        COMPLETED = "completed", _("Completed")
        CANCELLED = "cancelled", _("Cancelled")
        NO_SHOW = "no_show", _("No-show")

    pairing = models.ForeignKey(
        MentorshipPairing, on_delete=models.CASCADE, related_name="sessions"
    )
    track = models.ForeignKey(
        MentorshipTrack, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    topic = models.CharField(max_length=160, blank=True)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    duration_minutes = models.PositiveIntegerField(default=30)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SCHEDULED)
    location_hint = models.CharField(max_length=120, blank=True)
    # Optional: a solar system to poll live presence against during the window.
    location_system_id = models.BigIntegerField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    presence_checked_at = models.DateTimeField(null=True, blank=True)
    presence_result = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-scheduled_at", "-created_at"]

    def __str__(self) -> str:
        return f"Session<{self.pairing_id}:{self.status}>"


class MentorshipSessionParticipant(models.Model):
    class Role(models.TextChoices):
        MENTOR = "mentor", _("Mentor")
        MENTEE = "mentee", _("Mentee")
        OBSERVER = "observer", _("Observer")

    session = models.ForeignKey(
        MentorshipSession, on_delete=models.CASCADE, related_name="participants"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+"
    )
    role = models.CharField(max_length=8, choices=Role.choices, default=Role.MENTEE)
    confirmed = models.BooleanField(default=False)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    # Live presence poll result (null = not polled / unknown).
    present = models.BooleanField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["session", "user"], name="uniq_mentorship_session_participant")
        ]

    def __str__(self) -> str:
        return f"Part<{self.session_id}:{self.user_id}:{self.role}>"


# ---------------------------------------------------------------------------
# Rewards & recognition (ledger)
# ---------------------------------------------------------------------------
class MentorshipRewardLedger(TimeStampedModel):
    """A recorded reward. Nothing here moves ISK — an officer marks it paid with a
    free-text reference, exactly like SRP. Append-only in spirit; status advances
    through the approval/payment workflow and is stamped with who/when."""

    class Role(models.TextChoices):
        MENTOR = "mentor", _("Mentor")
        MENTEE = "mentee", _("Mentee")

    class Status(models.TextChoices):
        NOT_ELIGIBLE = "not_eligible", _("Not eligible")
        ELIGIBLE = "eligible", _("Eligible")
        PENDING_VALIDATION = "pending_validation", _("Pending validation")
        PENDING_APPROVAL = "pending_approval", _("Pending approval")
        APPROVED = "approved", _("Approved")
        PAID = "paid", _("Paid")
        REJECTED = "rejected", _("Rejected")
        CANCELLED = "cancelled", _("Cancelled")
        EXPIRED = "expired", _("Expired")

    OPEN_STATUSES = frozenset({
        Status.ELIGIBLE, Status.PENDING_VALIDATION, Status.PENDING_APPROVAL, Status.APPROVED,
    })

    rule = models.ForeignKey(
        MentorshipRewardRule, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="ledger_entries",
    )
    rule_key = models.CharField(max_length=80, blank=True)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="mentorship_rewards"
    )
    recipient_role = models.CharField(max_length=8, choices=Role.choices)
    pairing = models.ForeignKey(
        MentorshipPairing, on_delete=models.SET_NULL, null=True, blank=True, related_name="rewards"
    )
    assignment = models.ForeignKey(
        MentorshipTaskAssignment, on_delete=models.SET_NULL, null=True, blank=True, related_name="rewards"
    )
    trigger = models.CharField(max_length=20, blank=True)
    trigger_ref = models.CharField(max_length=80, blank=True)

    reward_type = models.CharField(max_length=8, choices=MentorshipRewardRule.RewardType.choices)
    amount = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    points = models.IntegerField(default=0)
    badge = models.ForeignKey(
        MentorshipBadge, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    title_text = models.CharField(max_length=120, blank=True)
    description = models.CharField(max_length=240, blank=True)

    validation_method = models.CharField(max_length=16, blank=True)
    confidence = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ELIGIBLE, db_index=True
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    payment_reference = models.CharField(max_length=200, blank=True)
    reason = models.CharField(max_length=300, blank=True)
    # Frozen copy of the rule at grant time so later edits don't rewrite history.
    rule_snapshot = models.JSONField(default=dict, blank=True)
    # Stable idempotency key: one grant per (rule, recipient, pairing, trigger_ref,
    # assignment/repeat). Empty for ad-hoc manual grants (always created).
    dedupe_key = models.CharField(max_length=200, blank=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "status"]),
            models.Index(fields=["status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["dedupe_key"],
                condition=models.Q(dedupe_key__gt=""),
                name="uniq_mentorship_reward_dedupe",
            )
        ]

    def __str__(self) -> str:
        return f"Reward<{self.recipient_id}:{self.reward_type}:{self.status}>"

    @property
    def is_isk(self) -> bool:
        return self.reward_type == MentorshipRewardRule.RewardType.ISK

    @property
    def description_i18n(self) -> str:
        """``description`` (a copied reward-rule label) under the reader's locale while unedited."""
        return tracks_i18n.reward_label(self.rule_key, self.description)


class MentorshipBadgeAward(models.Model):
    """A badge granted to a pilot (cosmetic recognition)."""

    badge = models.ForeignKey(
        MentorshipBadge, on_delete=models.CASCADE, related_name="awards"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="mentorship_badges"
    )
    pairing = models.ForeignKey(
        MentorshipPairing, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    awarded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    reason = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["badge", "user"], name="uniq_mentorship_badge_award")
        ]

    def __str__(self) -> str:
        return f"BadgeAward<{self.badge_id}:{self.user_id}>"

    @property
    def reason_i18n(self) -> str:
        """``reason`` under the reader's locale while it still matches a shipped reward-rule label."""
        return tracks_i18n.reward_reason(self.reason)


# ---------------------------------------------------------------------------
# Trust / anti-abuse
# ---------------------------------------------------------------------------
class MentorshipFlag(TimeStampedModel):
    """An anomaly worth a leader's eyes (append-only until resolved)."""

    class Kind(models.TextChoices):
        RAPID_COMPLETION = "rapid_completion", _("Tasks completed unusually fast")
        SELF_CONFIRM_STREAK = "self_confirm_streak", _("Long streak of self-confirmed tasks")
        MENTOR_RUBBER_STAMP = "mentor_rubber_stamp", _("Mentor approving without review")
        ALT_SUSPICION = "alt_suspicion", _("Possible alt / self-pairing")
        CAPACITY_EXCEEDED = "capacity_exceeded", _("Mentor over capacity")
        REVERSED_CREDIT = "reversed_credit", _("An internal credit was reversed")
        CAP_HIT = "cap_hit", _("Reward cap reached")
        STALE_PAIR = "stale_pair", _("Pair inactive")

    kind = models.CharField(max_length=24, choices=Kind.choices, db_index=True)
    severity = models.PositiveIntegerField(default=50, help_text=_("0–100."))
    pairing = models.ForeignKey(
        MentorshipPairing, on_delete=models.SET_NULL, null=True, blank=True, related_name="flags"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    detail = models.CharField(max_length=300, blank=True)
    # Seam B: the reader-locale form of ``detail``. Every flag is raised by the anomaly sweep
    # (a Celery worker with no locale) and read by whichever officer opens the pairing.
    detail_key = models.CharField(**_KEY_FIELD_KWARGS)
    detail_params = models.JSONField(default=dict, blank=True, db_default=_PARAMS_DB_DEFAULT)
    # Stable key so the anomaly sweep upserts rather than duplicating. NEVER translated: it is a
    # uniqueness lookup (``uniq_mentorship_open_flag``), not prose.
    dedupe_key = models.CharField(max_length=200, blank=True, db_index=True)
    resolved = models.BooleanField(default=False)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["dedupe_key"],
                condition=models.Q(dedupe_key__gt="", resolved=False),
                name="uniq_mentorship_open_flag",
            )
        ]

    def __str__(self) -> str:
        return f"Flag<{self.kind}:{self.severity}>"

    @property
    def detail_i18n(self) -> str:
        """``detail`` under the reader's locale; the stored English when there is no key."""
        return msg.render(self.detail_key, self.detail_params, self.detail)
