"""Capsuleer Path suggestion engine (doc 08) — pilot-scoped, rule-based, explainable advice.

Eight named, pure-rule generators (no ML, no scoring model — spec §15) produce ``PathSuggestion``
rows the pilot alone ever sees. Each row explains itself: ``reason`` is mandatory human copy, ``data``
carries the inputs and their as-of stamps, ``corp_driven`` is a rendered label. The daily beat
(``capsuleer.generate_suggestions``) loads each user's context once, runs the generators in the fixed
kind order (skipping muted kinds), gates them against the pilot's ``corp_alignment`` and
``avoided_activities``, expires open rows whose condition cleared, upserts the rest preserving pilot
state (the ``PilotRecommendation`` idiom), and — under the storm caps — admits new rows and emits at
most one count-only DM.

Suggestions never mutate goals, never create tasks, never enrol the pilot in anything. Accepting one
at most materialises a ``CareerActionStep(source=suggestion)`` on the pilot's own goal and hands the
UI a redirect target. Leadership can neither see the inbox nor inject into it (no write path exists).

Every integration read (operations, mentorship, store, campaigns) is defensive: a source app with no
data, or that raises, yields no suggestion — never an error, never unknown-spam.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import config, messages, notify, progress
from .models import (
    CareerActionStep,
    CareerGoal,
    CareerProfile,
    GoalStatus,
    PathSuggestion,
    StepSource,
    SuggestionKind,
    SuggestionStatus,
)
from .taxonomy import campaign_categories_for, mentorship_category_for, operation_types_for

logger = logging.getLogger("forca.capsuleer")

# Fixed kind priority (doc 08 §5.9): opportunities first, goal-health nudges last.
_KIND_ORDER = [
    SuggestionKind.BLOCKED_PREREQ,
    SuggestionKind.EVENT_MATCH,
    SuggestionKind.CAMPAIGN_OPPORTUNITY,
    SuggestionKind.NEAR_QUALIFICATION,
    SuggestionKind.SHIP_AVAILABLE,
    SuggestionKind.MENTOR_AVAILABLE,
    SuggestionKind.REVIEW_DUE,
    SuggestionKind.STALLED_GOAL,
]

# Condition-bound kinds: an open row whose trigger no longer holds is expired (doc 08 §3 step 4).
# The bucketed kinds (mentor_available, stalled_goal, review_due) instead rotate by month.
_CONDITION_BOUND = frozenset({
    SuggestionKind.NEAR_QUALIFICATION, SuggestionKind.BLOCKED_PREREQ, SuggestionKind.SHIP_AVAILABLE,
    SuggestionKind.EVENT_MATCH, SuggestionKind.CAMPAIGN_OPPORTUNITY,
})

# Storm caps (doc 08 §8). ``max_open_per_user`` is the one config-tunable; the rest are floors.
_PER_RUN_CREATE_CAP = 3
_PER_GOAL_OPEN_CAP = 2

# A far-future, tz-aware sentinel so rows with no expiry sort last.
_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)

# Corp priority doctrines are combat/PvP fleet content, so goalless doctrine widening is combat-
# flavoured: it only reaches a pilot who prefers a combat activity and has not avoided it (doc 08 §6,
# Scenario C.1, finding 28). A doctrine has no clean single-activity mapping, so the whole combat
# family is the honoured proxy — a combat-avoider never receives goalless doctrine suggestions.
_COMBAT_ACTIVITIES = frozenset({
    "combat_line", "combat_support", "tackle_scout", "fleet_command", "black_ops", "capitals",
})


@dataclass
class Draft:
    """One suggestion to upsert.

    ``title`` / ``reason`` are the English prose that is persisted (the audit record, and what a
    reader falls back to). ``*_key`` / ``*_params`` are the same two sentences as message scaffolds
    (:mod:`apps.capsuleer.messages`): this whole module runs in the daily Celery beat, which has no
    reader and no locale, so a ``gettext``/``gettext_lazy`` here would simply freeze the row in the
    worker's English — only a key + raw params can be re-rendered later in the *pilot's* language.
    Build both with :func:`_prose`, never by hand, so they cannot drift apart.
    """

    kind: str
    dedupe_key: str
    goal_id: int | None
    title: str
    reason: str
    data: dict
    corp_driven: bool
    expires_at: datetime | None
    title_key: str = ""
    title_params: dict = field(default_factory=dict)
    reason_key: str = ""
    reason_params: dict = field(default_factory=dict)


def _prose(key: str, title_params: dict, reason_params: dict) -> dict:
    """The ``title`` / ``reason`` (English) + their scaffold keys and params, as ``Draft`` kwargs.

    Every param value must be plain JSON — ints, strings, or nested ``{"text","key","params"}``
    blocker refs. Never a lazy proxy (a ``JSONField`` write would raise ``TypeError``), never a
    model instance, never a ``Decimal``.
    """
    title, title_key, title_params = messages.english(f"{key}.title", **title_params)
    reason, reason_key, reason_params = messages.english(f"{key}.reason", **reason_params)
    return {"title": title, "title_key": title_key, "title_params": title_params,
            "reason": reason, "reason_key": reason_key, "reason_params": reason_params}


# --------------------------------------------------------------------------- #
#  Per-user context
# --------------------------------------------------------------------------- #
@dataclass
class SuggestContext:
    user: object
    profile: CareerProfile | None
    goals: list  # active + paused, milestones prefetched
    now: datetime
    _snap_cache: dict = field(default_factory=dict)
    _asset_ctx_cache: dict = field(default_factory=dict)
    _failed_kinds: set = field(default_factory=set)

    @property
    def uid(self) -> int:
        return self.user.pk

    def mark_failed(self, kind) -> None:
        """Record that a generator's evidence source failed this run, so ``_expire_cleared`` never
        treats its still-valid open rows as condition-cleared (finding 15)."""
        self._failed_kinds.add(kind)

    @property
    def failed_kinds(self) -> set:
        return self._failed_kinds

    @property
    def active_goals(self) -> list:
        return [g for g in self.goals if g.status == GoalStatus.ACTIVE]

    @property
    def alignment(self) -> str:
        return self.profile.corp_alignment if self.profile else "balanced"

    @property
    def muted(self) -> set:
        return set(self.profile.suggestion_muted_kinds) if self.profile else set()

    @property
    def avoided(self) -> set:
        return set(self.profile.avoided_activities) if self.profile else set()

    @property
    def preferred(self) -> set:
        return set(self.profile.preferred_activities) if self.profile else set()

    @property
    def curious(self) -> set:
        return set(self.profile.curious_activities) if self.profile else set()

    def snapshot(self, character):
        if character is None:
            return None
        cid = character.character_id
        if cid not in self._snap_cache:
            self._snap_cache[cid] = character.skill_snapshots.filter(is_latest=True).first()
        return self._snap_cache[cid]

    def asset_context(self, character):
        """A ``verify.CheckContext`` cached per character so multiple goals on one character share a
        single asset load instead of re-aggregating per goal (finding 26)."""
        from . import verify

        cid = character.character_id
        if cid not in self._asset_ctx_cache:
            self._asset_ctx_cache[cid] = verify.context_for(character)
        return self._asset_ctx_cache[cid]


def _build_context(user, now) -> SuggestContext:
    profile = CareerProfile.objects.filter(user=user).first()
    goals = list(
        CareerGoal.objects.filter(
            user=user, status__in=[GoalStatus.ACTIVE, GoalStatus.PAUSED]
        ).select_related("character", "template").prefetch_related("milestones")
    )
    return SuggestContext(user=user, profile=profile, goals=goals, now=now)


def _dk(uid, kind, subject_type, subject_id, bucket=None) -> str:
    key = f"u{uid}:{kind}:{subject_type}:{subject_id}"
    return f"{key}:{bucket}" if bucket else key


def _month(now) -> str:
    return now.strftime("%Y-%m")


def _goal_doctrine_id(goal) -> int | None:
    if goal.doctrine_id:
        return goal.doctrine_id
    if goal.template_id and goal.template and goal.template.doctrine_id:
        return goal.template.doctrine_id
    return None


def _goal_activity(goal) -> str:
    if goal.activity:
        return goal.activity
    if goal.template_id and goal.template:
        return goal.template.category
    return ""


def _goal_hull(goal) -> int | None:
    if goal.ship_type_id:
        return goal.ship_type_id
    doctrine_id = _goal_doctrine_id(goal)
    if not doctrine_id:
        return None
    try:
        from apps.doctrines.models import Doctrine

        doctrine = Doctrine.objects.filter(id=doctrine_id).prefetch_related("fits").first()
        if doctrine is None:
            return None
        fits = sorted(doctrine.fits.all(), key=lambda f: (f.is_cheap_alt, f.id))
        return fits[0].ship_type_id if fits else None
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
#  Generators (doc 08 §5)
# --------------------------------------------------------------------------- #
def gen_near_qualification(ctx) -> list[Draft]:
    drafts = []
    try:
        from apps.doctrines.models import Doctrine
        from apps.skills.services import (
            collect_missing_for_doctrine,
            estimate_seconds_to_doctrine,
        )
    except Exception:  # noqa: BLE001
        ctx.mark_failed(SuggestionKind.NEAR_QUALIFICATION)
        return drafts
    covered = set()
    for goal in ctx.active_goals:
        doctrine_id = _goal_doctrine_id(goal)
        char = goal.character
        if not doctrine_id or char is None:
            continue
        covered.add(doctrine_id)
        snap = ctx.snapshot(char)
        if snap is None:  # never fabricate closeness (doc 08 §5.1)
            continue
        result = _near_qual_for(ctx, char, doctrine_id, snap, Doctrine,
                                collect_missing_for_doctrine, estimate_seconds_to_doctrine)
        if result is None:
            continue
        doctrine, total_gap, days = result
        drafts.append(Draft(
            kind=SuggestionKind.NEAR_QUALIFICATION,
            dedupe_key=_dk(ctx.uid, SuggestionKind.NEAR_QUALIFICATION, "doctrine", doctrine_id),
            goal_id=goal.pk,
            **_prose(
                messages.SUG_NEAR_QUAL,
                {"doctrine": doctrine.name},
                {"levels": total_gap, "days": days, "doctrine": doctrine.name,
                 "goal": goal.title, "as_of": f"{snap.as_of:%Y-%m-%d}"},
            ),
            data={"inputs": {"doctrine_id": doctrine_id, "doctrine": doctrine.name,
                             "missing_levels": total_gap, "days": days},
                  "as_of": {"skills": snap.as_of.isoformat()}, "corp_demand": {"present": False}},
            corp_driven=False,
            expires_at=ctx.now + timedelta(days=30),
        ))
    # Goalless widening (doc 08 §6): under corp_forward/show_all, surface high-priority corp
    # doctrines the pilot's main is close to even without a goal, corp_driven — but only when the
    # pilot actually prefers combat and hasn't avoided it (finding 28). corp_forward matches on
    # preferred; show_all also admits curious.
    interests = ctx.preferred | (ctx.curious if ctx.alignment == "show_all" else set())
    combat_interest = (interests & _COMBAT_ACTIVITIES) - ctx.avoided
    if ctx.alignment in ("corp_forward", "show_all") and combat_interest:
        char = _main_character(ctx)
        snap = ctx.snapshot(char)
        if char is not None and snap is not None:
            for doctrine in Doctrine.objects.filter(
                status=Doctrine.Status.ACTIVE, priority__gt=0
            ).order_by("-priority")[:10]:
                if doctrine.id in covered:
                    continue
                result = _near_qual_for(ctx, char, doctrine.id, snap, Doctrine,
                                        collect_missing_for_doctrine, estimate_seconds_to_doctrine)
                if result is None:
                    continue
                _d, total_gap, days = result
                drafts.append(Draft(
                    kind=SuggestionKind.NEAR_QUALIFICATION,
                    dedupe_key=_dk(ctx.uid, SuggestionKind.NEAR_QUALIFICATION, "doctrine", doctrine.id),
                    goal_id=None,
                    **_prose(
                        messages.SUG_NEAR_QUAL_CORP,
                        {"doctrine": doctrine.name},
                        {"levels": total_gap, "days": days, "doctrine": doctrine.name,
                         "priority": doctrine.priority},
                    ),
                    data={"inputs": {"doctrine_id": doctrine.id, "doctrine": doctrine.name,
                                     "missing_levels": total_gap, "days": days},
                          "as_of": {"skills": snap.as_of.isoformat()},
                          "corp_demand": {"present": True, "signal": "doctrine_priority",
                                          "doctrine_id": doctrine.id}},
                    corp_driven=True,
                    expires_at=ctx.now + timedelta(days=30),
                ))
    return drafts


def _near_qual_for(ctx, char, doctrine_id, snap, Doctrine, collect_missing, estimate_seconds):
    """``(doctrine, total_gap, days)`` when the character is close to the doctrine, else ``None``."""
    try:
        doctrine = Doctrine.objects.filter(id=doctrine_id, status=Doctrine.Status.ACTIVE).first()
        if doctrine is None:
            return None
        missing = collect_missing(char, doctrine, snapshot=snap)
        if not missing:
            return None
        total_gap = sum(max(0, need - snap.trained_level(sid)) for sid, need in missing.items())
        days = int(estimate_seconds(char, doctrine, snapshot=snap) / 86400)
        if total_gap > 3 and days > 14:
            return None
        return doctrine, total_gap, days
    except Exception:  # noqa: BLE001
        return None


def _main_character(ctx):
    from core import pilots

    return pilots.acting_pilot(ctx.user)  # LP-3: the pilot the user is FLYING, not the account's main.


def gen_event_match(ctx) -> list[Draft]:
    drafts = []
    try:
        from apps.operations.models import Operation, OperationCommitment
    except Exception:  # noqa: BLE001
        ctx.mark_failed(SuggestionKind.EVENT_MATCH)
        return drafts
    horizon = ctx.now + timedelta(days=7)
    try:
        ops = list(Operation.objects.filter(
            status=Operation.Status.PLANNED, target_at__gt=ctx.now, target_at__lte=horizon,
        ))
    except Exception:  # noqa: BLE001
        ctx.mark_failed(SuggestionKind.EVENT_MATCH)
        return drafts
    # One fetch of this pilot's commitments, not a per-(goal, op) EXISTS query (finding 23).
    try:
        committed = set(
            OperationCommitment.objects.filter(user=ctx.user).values_list("operation_id", flat=True)
        )
    except Exception:  # noqa: BLE001
        committed = set()
    covered_ops = set()
    for goal in ctx.active_goals:
        activity = _goal_activity(goal)
        if not activity or activity in ctx.avoided:
            continue
        types = operation_types_for(activity)
        if not types:
            continue
        for op in ops:
            if op.type not in types or not op.is_open_for_signup:
                continue
            if op.pk in committed:
                continue
            covered_ops.add(op.pk)
            drafts.append(Draft(
                kind=SuggestionKind.EVENT_MATCH,
                dedupe_key=_dk(ctx.uid, SuggestionKind.EVENT_MATCH, "operation", op.pk),
                goal_id=goal.pk,
                # ``activity`` is a taxonomy slug and ``op_type`` the operations app's own display
                # label — both stay raw; only the sentence around them is translated.
                **_prose(
                    messages.SUG_EVENT_MATCH,
                    {"operation": op.name, "activity": activity},
                    {"operation": op.name, "op_type": str(op.get_type_display()),
                     "when": f"{op.target_at:%Y-%m-%d %H:%M}", "goal": goal.title},
                ),
                data={"inputs": {"operation_id": op.pk, "op_type": op.type, "activity": activity},
                      "as_of": {"operations": {"generated_at": ctx.now.isoformat()}},
                      "corp_demand": {"present": False}},
                corp_driven=False,
                expires_at=op.target_at,
            ))
    # Goalless widening (doc 08 §6): under corp_forward/show_all, ops matching preferred/curious
    # activities even without a goal (avoided always honoured).
    if ctx.alignment in ("corp_forward", "show_all"):
        interests = ctx.preferred | (ctx.curious if ctx.alignment == "show_all" else set())
        interests -= ctx.avoided
        want_types = set()
        for activity in interests:
            want_types |= operation_types_for(activity)
        for op in ops:
            if op.pk in covered_ops or op.type not in want_types or not op.is_open_for_signup:
                continue
            if op.pk in committed:
                continue
            drafts.append(Draft(
                kind=SuggestionKind.EVENT_MATCH,
                dedupe_key=_dk(ctx.uid, SuggestionKind.EVENT_MATCH, "operation", op.pk),
                goal_id=None,
                **_prose(
                    messages.SUG_EVENT_MATCH_INTEREST,
                    {"operation": op.name},
                    {"operation": op.name, "op_type": str(op.get_type_display()),
                     "when": f"{op.target_at:%Y-%m-%d %H:%M}"},
                ),
                data={"inputs": {"operation_id": op.pk, "op_type": op.type},
                      "as_of": {"operations": {"generated_at": ctx.now.isoformat()}},
                      "corp_demand": {"present": False}},
                corp_driven=False,
                expires_at=op.target_at,
            ))
    return drafts


def gen_mentor_available(ctx) -> list[Draft]:
    drafts = []
    if not (ctx.profile and ctx.profile.mentor_interest):
        return drafts
    try:
        from django.db.models import Count, Q

        from apps.mentorship.models import MentorProfile, MentorshipPairing, MentorshipTrack
        from apps.mentorship.services import active_program, program_open

        if not program_open():
            return drafts
        # A pilot already in an open pairing does not need a mentor nudge.
        if MentorshipPairing.objects.filter(
            mentee__user=ctx.user, status__in=MentorshipPairing.OPEN_STATUSES
        ).exists():
            return drafts
        # Capacity is annotated once, so the per-goal count is a field comparison rather than a
        # COUNT per mentor per goal; the program's default cap is read once rather than through
        # ``mentor_capacity`` per mentor (which re-selects the uncached program) — finding 23.
        default_cap = active_program().max_active_mentees_per_mentor
        active_mentors = list(
            MentorProfile.objects.filter(status=MentorProfile.Status.ACTIVE).annotate(
                _active_pairs=Count(
                    "pairings", filter=Q(pairings__status__in=MentorshipPairing.CAPACITY_STATUSES)
                )
            )
        )
        available = [
            m for m in active_mentors if m._active_pairs < (m.max_active_mentees or default_cap)
        ]
    except Exception:  # noqa: BLE001
        return drafts
    for goal in ctx.active_goals:
        activity = _goal_activity(goal)
        if not activity:
            continue
        category = mentorship_category_for(activity)
        if category == MentorshipTrack.Category.OTHER:  # unmapped → nothing (doc 08 §5.3)
            continue
        count = sum(1 for m in available if category in set(m.areas or []))
        if count < 1:
            continue
        drafts.append(Draft(
            kind=SuggestionKind.MENTOR_AVAILABLE,
            dedupe_key=_dk(ctx.uid, SuggestionKind.MENTOR_AVAILABLE, "goal", goal.pk, _month(ctx.now)),
            goal_id=goal.pk,
            **_prose(
                messages.SUG_MENTOR_AVAILABLE,
                {"goal": goal.title},
                {"count": count, "category": str(category), "goal": goal.title},
            ),
            data={"inputs": {"category": category, "mentor_count": count},
                  "as_of": {"mentorship": {"generated_at": ctx.now.isoformat()}},
                  "corp_demand": {"present": False}},
            corp_driven=False,
            expires_at=_end_of_month(ctx.now),
        ))
    return drafts


def gen_stalled_goal(ctx) -> list[Draft]:
    drafts = []
    for goal in ctx.goals:
        if goal.status != GoalStatus.ACTIVE:
            continue
        idle = ctx.now - progress._last_movement_at(goal)
        if idle.days < progress.STALLED_AFTER_DAYS:
            continue
        if _open_row_exists(ctx.user, SuggestionKind.REVIEW_DUE, goal.pk, ctx.now):
            continue  # one nudge at a time (doc 08 §5.4)
        drafts.append(Draft(
            kind=SuggestionKind.STALLED_GOAL,
            dedupe_key=_dk(ctx.uid, SuggestionKind.STALLED_GOAL, "goal", goal.pk, _month(ctx.now)),
            goal_id=goal.pk,
            **_prose(
                messages.SUG_STALLED_GOAL,
                {"goal": goal.title},
                {"goal": goal.title, "weeks": idle.days // 7},
            ),
            data={"inputs": {"idle_days": idle.days},
                  "as_of": {"capsuleer": {"generated_at": ctx.now.isoformat()}},
                  "corp_demand": {"present": False}},
            corp_driven=False,
            expires_at=_end_of_month(ctx.now),
        ))
    return drafts


def gen_blocked_prereq(ctx) -> list[Draft]:
    drafts = []
    for goal in ctx.active_goals:
        try:
            refs = progress.blocked_refs(goal)
        except Exception:  # noqa: BLE001
            ctx.mark_failed(SuggestionKind.BLOCKED_PREREQ)
            continue
        if not refs:
            continue
        reasons = [messages.text(r["text"], r["key"], r["params"]) for r in refs]
        drafts.append(Draft(
            kind=SuggestionKind.BLOCKED_PREREQ,
            dedupe_key=_dk(ctx.uid, SuggestionKind.BLOCKED_PREREQ, "goal", goal.pk),
            goal_id=goal.pk,
            # The blockers are themselves scaffold refs, so ``%(blockers)s`` localises with the
            # reader too instead of embedding the sweep's frozen English inside the sentence.
            **_prose(
                messages.SUG_BLOCKED_PREREQ,
                {"goal": goal.title},
                {"goal": goal.title, "blockers": refs},
            ),
            data={"inputs": {"blockers": reasons},
                  "as_of": {"capsuleer": {"generated_at": ctx.now.isoformat()}},
                  "corp_demand": {"present": False}},
            corp_driven=False,
            expires_at=None,
        ))
    return drafts


def gen_ship_available(ctx) -> list[Draft]:
    drafts = []
    try:
        from decimal import Decimal

        from apps.store.pricing import price_hull
    except Exception:  # noqa: BLE001
        ctx.mark_failed(SuggestionKind.SHIP_AVAILABLE)
        return drafts
    for goal in ctx.active_goals:
        hull = _goal_hull(goal)
        char = goal.character
        if not hull or char is None:
            continue
        try:
            asset_ctx = ctx.asset_context(char)
            if asset_ctx.assets_as_of is None:  # no asset scope → skip, never "not owned"
                continue
            if asset_ctx.owned.get(hull, 0) >= 1:  # reuse the one asset load (finding 26)
                continue  # already owns it → condition cleared
            priced = price_hull(hull, Decimal("1.0"))
            if not getattr(priced, "ok", False) or (priced.unit_price or 0) <= 0:
                continue  # unknown price never renders as "free" (doc 08 §5.6)
        except Exception:  # noqa: BLE001, S112
            continue
        within = _within_budget(ctx, priced.unit_price)
        # Two whole scaffolds rather than one sentence plus an appended fragment: a translator must
        # never be handed half a sentence to concatenate.
        key = messages.SUG_SHIP_AVAILABLE_BUDGET if within else messages.SUG_SHIP_AVAILABLE
        drafts.append(Draft(
            kind=SuggestionKind.SHIP_AVAILABLE,
            dedupe_key=_dk(ctx.uid, SuggestionKind.SHIP_AVAILABLE, "ship", hull),
            goal_id=goal.pk,
            **_prose(key, {}, {"ship": priced.ship_name, "goal": goal.title}),
            # data minimisation: a within-budget boolean at most, never the ISK figure (doc 08 §5.6).
            data={"inputs": {"ship_type_id": hull, "ship_name": priced.ship_name,
                             "within_budget": within},
                  "as_of": {"assets": asset_ctx.assets_as_of.isoformat(),
                            "prices": {"generated_at": ctx.now.isoformat()}},
                  "corp_demand": {"present": False}},
            corp_driven=False,
            expires_at=ctx.now + timedelta(days=14),
        ))
    return drafts


def gen_campaign_opportunity(ctx) -> list[Draft]:
    drafts = []
    try:
        from apps.campaigns.models import Campaign
        from apps.campaigns.services import visible_campaigns
    except Exception:  # noqa: BLE001
        ctx.mark_failed(SuggestionKind.CAMPAIGN_OPPORTUNITY)
        return drafts
    activities = {_goal_activity(g) for g in ctx.active_goals} | ctx.preferred
    activities = {a for a in activities if a and a not in ctx.avoided}
    if not activities:
        return drafts
    wanted_categories = set()
    for activity in activities:
        wanted_categories |= campaign_categories_for(activity)
    if not wanted_categories:
        return drafts
    try:
        campaigns = visible_campaigns(ctx.user).filter(status=Campaign.Status.ACTIVE)
    except Exception:  # noqa: BLE001
        ctx.mark_failed(SuggestionKind.CAMPAIGN_OPPORTUNITY)
        return drafts
    for camp in campaigns:
        if camp.category not in wanted_categories:
            continue
        try:
            objective = camp.objectives.filter(help_wanted=True).first()
        except Exception:  # noqa: BLE001, S112
            continue
        if objective is None:
            continue
        matched = next((a for a in activities if camp.category in campaign_categories_for(a)), "")
        drafts.append(Draft(
            kind=SuggestionKind.CAMPAIGN_OPPORTUNITY,
            dedupe_key=_dk(ctx.uid, SuggestionKind.CAMPAIGN_OPPORTUNITY, "campaign", camp.pk),
            goal_id=None,
            **_prose(
                messages.SUG_CAMPAIGN_OPPORTUNITY,
                {"campaign": camp.name},
                {"campaign": camp.name, "objective": objective.title, "activity": matched},
            ),
            data={"inputs": {"campaign_id": camp.pk, "objective_id": objective.pk,
                             "activity": matched},
                  "as_of": {"campaigns": {"generated_at": ctx.now.isoformat()}},
                  "corp_demand": {"present": True, "signal": "help_wanted"}},
            corp_driven=True,
            expires_at=camp.target_end_at or (ctx.now + timedelta(days=30)),
        ))
    return drafts


def gen_review_due(ctx) -> list[Draft]:
    drafts = []
    for goal in ctx.goals:  # active + paused
        if goal.review_due_at and goal.review_due_at <= ctx.now:
            drafts.append(Draft(
                kind=SuggestionKind.REVIEW_DUE,
                dedupe_key=_dk(ctx.uid, SuggestionKind.REVIEW_DUE, "goal", goal.pk, _month(ctx.now)),
                goal_id=goal.pk,
                **_prose(
                    messages.SUG_REVIEW_DUE_GOAL,
                    {"goal": goal.title},
                    {"goal": goal.title},
                ),
                data={"inputs": {"review_due_at": goal.review_due_at.isoformat()},
                      "as_of": {"capsuleer": {"generated_at": ctx.now.isoformat()}},
                      "corp_demand": {"present": False}},
                corp_driven=False,
                expires_at=_end_of_month(ctx.now),
            ))
    if ctx.profile is not None:
        last = ctx.profile.last_reviewed_at
        overdue = (
            (last is None and (ctx.now - ctx.profile.created_at).days > 90)
            or (last is not None and (ctx.now - last).days > 90)
        )
        if overdue:
            drafts.append(Draft(
                kind=SuggestionKind.REVIEW_DUE,
                dedupe_key=_dk(ctx.uid, SuggestionKind.REVIEW_DUE, "profile", 0, _month(ctx.now)),
                goal_id=None,
                **_prose(messages.SUG_REVIEW_DUE_PROFILE, {}, {}),
                data={"inputs": {"last_reviewed_at": last.isoformat() if last else None},
                      "as_of": {"capsuleer": {"generated_at": ctx.now.isoformat()}},
                      "corp_demand": {"present": False}},
                corp_driven=False,
                expires_at=_end_of_month(ctx.now),
            ))
    return drafts


_GENERATORS = {
    SuggestionKind.NEAR_QUALIFICATION: gen_near_qualification,
    SuggestionKind.EVENT_MATCH: gen_event_match,
    SuggestionKind.MENTOR_AVAILABLE: gen_mentor_available,
    SuggestionKind.STALLED_GOAL: gen_stalled_goal,
    SuggestionKind.BLOCKED_PREREQ: gen_blocked_prereq,
    SuggestionKind.SHIP_AVAILABLE: gen_ship_available,
    SuggestionKind.CAMPAIGN_OPPORTUNITY: gen_campaign_opportunity,
    SuggestionKind.REVIEW_DUE: gen_review_due,
}


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def _end_of_month(now) -> datetime:
    if now.month == 12:
        return now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0,
                           microsecond=0)
    return now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)


def _within_budget(ctx, price) -> bool:
    if not ctx.profile or ctx.profile.monthly_budget_isk is None:
        return False
    try:
        return price <= ctx.profile.monthly_budget_isk
    except Exception:  # noqa: BLE001
        return False


def _open_qs(user, now):
    """A user's open, unexpired suggestion rows."""
    from django.db.models import Q

    return PathSuggestion.objects.filter(user=user, status=SuggestionStatus.OPEN).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now)
    )


def _open_row_exists(user, kind, goal_id, now) -> bool:
    return _open_qs(user, now).filter(kind=kind, goal_id=goal_id).exists()


# --------------------------------------------------------------------------- #
#  Alignment gating (doc 08 §6)
# --------------------------------------------------------------------------- #
def _apply_alignment(ctx, drafts) -> list[Draft]:
    alignment = ctx.alignment
    if alignment == "personal_only":
        # Every corp_driven draft is suppressed at generation — nothing corp-flavoured is created.
        return [d for d in drafts if not d.corp_driven]
    if alignment == "mostly_personal":
        # At most one open corp_driven row at any time (doc 08 §6 cross-cutting rule).
        corp_open = _open_qs(ctx.user, ctx.now).filter(corp_driven=True).count()
        out, allowed = [], max(0, 1 - corp_open)
        for d in drafts:
            if d.corp_driven:
                # An existing open row for this key refreshes freely; only genuinely new ones count.
                exists = PathSuggestion.objects.filter(user=ctx.user, dedupe_key=d.dedupe_key).exists()
                if not exists:
                    if allowed <= 0:
                        continue
                    allowed -= 1
            out.append(d)
        return out
    return drafts


# --------------------------------------------------------------------------- #
#  Upsert + storm caps (doc 08 §7, §8)
# --------------------------------------------------------------------------- #
def _order_drafts(drafts) -> list[Draft]:
    priority = {k: i for i, k in enumerate(_KIND_ORDER)}
    return sorted(drafts, key=lambda d: (priority.get(d.kind, 99),
                                         d.expires_at or _FAR_FUTURE))


def _expire_cleared(ctx, drafts) -> int:
    """Expire open condition-bound rows whose trigger no longer holds (doc 08 §3 step 4).

    A kind whose evidence source failed this run is never expired — a transient outage yields no
    suggestion, never destructive expiry of still-valid rows (finding 15)."""
    live_keys = {d.dedupe_key for d in drafts}
    kinds = _CONDITION_BOUND - ctx.failed_kinds
    if not kinds:
        return 0
    stale = _open_qs(ctx.user, ctx.now).filter(kind__in=kinds).exclude(
        dedupe_key__in=live_keys
    )
    return stale.update(expires_at=ctx.now)


def _suppress_stalled_when_review(drafts) -> list[Draft]:
    """Drop a STALLED_GOAL draft for any goal that also has a REVIEW_DUE draft this run — one nudge
    at a time, even on the first run after housekeeping flags the review (doc 08 §5.4, finding 16).
    The DB-only guard in ``gen_stalled_goal`` can't see a same-run REVIEW_DUE draft."""
    review_goals = {
        d.goal_id for d in drafts
        if d.kind == SuggestionKind.REVIEW_DUE and d.goal_id is not None
    }
    return [
        d for d in drafts
        if not (d.kind == SuggestionKind.STALLED_GOAL and d.goal_id in review_goals)
    ]


def _upsert(ctx, drafts, counts) -> int:
    max_open = int(config.get("suggestions").get("max_open_per_user", 6))
    open_rows = list(_open_qs(ctx.user, ctx.now))
    open_count = len(open_rows)
    per_goal = {}
    for row in open_rows:
        per_goal[row.goal_id] = per_goal.get(row.goal_id, 0) + 1

    created = 0
    for draft in _order_drafts(drafts):
        counts["drafts"] = counts.get("drafts", 0) + 1
        existing = PathSuggestion.objects.filter(user=ctx.user, dedupe_key=draft.dedupe_key).first()
        if existing is not None:
            if existing.status != SuggestionStatus.OPEN:
                continue  # acted state is never overwritten (doc 08 §7)
            _refresh_open(existing, draft)  # content refresh does not count against caps
            continue
        # A brand-new row is subject to every cap.
        if open_count >= max_open or created >= _PER_RUN_CREATE_CAP:
            counts["capped"] = counts.get("capped", 0) + 1
            continue
        if draft.goal_id is not None and per_goal.get(draft.goal_id, 0) >= _PER_GOAL_OPEN_CAP:
            counts["capped"] = counts.get("capped", 0) + 1
            continue
        if _create_draft(ctx, draft):
            created += 1
            open_count += 1
            per_goal[draft.goal_id] = per_goal.get(draft.goal_id, 0) + 1
    counts["admitted"] = counts.get("admitted", 0) + created
    return created


def _refresh_open(existing, draft) -> None:
    existing.title = draft.title[:140]
    existing.reason = draft.reason
    # The key/params travel with the prose on every refresh — a row must never end up holding a
    # stale key that describes a sentence it no longer stores.
    existing.title_key = draft.title_key
    existing.title_params = draft.title_params
    existing.reason_key = draft.reason_key
    existing.reason_params = draft.reason_params
    existing.data = draft.data
    existing.corp_driven = draft.corp_driven
    existing.expires_at = draft.expires_at
    existing.save(update_fields=["title", "reason", "title_key", "title_params", "reason_key",
                                 "reason_params", "data", "corp_driven", "expires_at",
                                 "updated_at"])


def _create_draft(ctx, draft) -> bool:
    try:
        with transaction.atomic():
            PathSuggestion.objects.create(
                user=ctx.user, goal_id=draft.goal_id, kind=draft.kind, title=draft.title[:140],
                reason=draft.reason, title_key=draft.title_key, title_params=draft.title_params,
                reason_key=draft.reason_key, reason_params=draft.reason_params,
                data=draft.data, corp_driven=draft.corp_driven,
                dedupe_key=draft.dedupe_key, expires_at=draft.expires_at,
                status=SuggestionStatus.OPEN,
            )
        return True
    except IntegrityError:
        # A dedupe race: the row now exists — leave it (single-retry-as-noop, doc 08 §7).
        return False


# --------------------------------------------------------------------------- #
#  Generation pipeline (doc 08 §3) — the beat body
# --------------------------------------------------------------------------- #
def run_generation(now=None) -> dict:
    """``capsuleer.generate_suggestions`` beat body. Per user with a profile or ≥1 active/paused
    goal, batched with try/except isolation (``warm_pilots`` pattern). Idempotent: a second run the
    same day refreshes open rows and admits nothing new."""
    now = now or timezone.now()
    counts = {"users": 0, "drafts": 0, "admitted": 0, "capped": 0, "expired": 0, "errors": 0}
    user_ids = set(
        CareerProfile.objects.values_list("user_id", flat=True)
    ) | set(
        CareerGoal.objects.filter(
            status__in=[GoalStatus.ACTIVE, GoalStatus.PAUSED]
        ).values_list("user_id", flat=True)
    )
    from django.contrib.auth import get_user_model

    users = {u.pk: u for u in get_user_model().objects.filter(pk__in=user_ids)}
    for uid in user_ids:
        user = users.get(uid)
        if user is None:
            continue
        counts["users"] += 1
        try:
            _generate_for_user(user, now, counts)
        except Exception:  # noqa: BLE001 — one pilot's failure never aborts the sweep
            counts["errors"] += 1
            logger.exception("capsuleer suggestion generation failed for user %s", uid)
    logger.info("capsuleer.generate_suggestions %s", counts)
    return counts


def _generate_for_user(user, now, counts) -> None:
    ctx = _build_context(user, now)
    if ctx.profile is None and not ctx.goals:
        return
    muted = ctx.muted
    drafts = []
    for kind in _KIND_ORDER:
        if kind in muted:
            continue
        drafts.extend(_run_generator_safely(kind, ctx))
    drafts = _apply_alignment(ctx, drafts)
    drafts = _suppress_stalled_when_review(drafts)
    counts["expired"] = counts.get("expired", 0) + _expire_cleared(ctx, drafts)
    created = _upsert(ctx, drafts, counts)
    if created > 0:
        notify.suggestion_batch(user.pk, created, day=now.strftime("%Y-%m-%d"))


def _run_generator_safely(kind, ctx) -> list[Draft]:
    try:
        return _GENERATORS[kind](ctx)
    except Exception:  # noqa: BLE001 — a misfiring generator never breaks the run
        logger.exception("capsuleer generator %s failed for user %s", kind, ctx.uid)
        return []


# --------------------------------------------------------------------------- #
#  Display + action service (doc 08 §7)
# --------------------------------------------------------------------------- #
_DEFER_DAYS = 14


def inbox_suggestions(user, now=None) -> list:
    """The suggestions the pilot's inbox shows: open + unexpired, plus deferred rows whose 14-day
    hide window has elapsed (doc 08 §7 defer semantics). Ordered by kind priority then expiry."""
    from django.db.models import Q

    now = now or timezone.now()
    defer_cut = now - timedelta(days=_DEFER_DAYS)
    rows = list(
        PathSuggestion.objects.filter(user=user).filter(
            (Q(status=SuggestionStatus.OPEN) & (Q(expires_at__isnull=True) | Q(expires_at__gt=now)))
            | (Q(status=SuggestionStatus.DEFERRED) & Q(acted_at__lte=defer_cut))
        )
    )
    priority = {k: i for i, k in enumerate(_KIND_ORDER)}
    rows.sort(key=lambda s: (priority.get(s.kind, 99),
                             s.expires_at or _FAR_FUTURE))
    return rows


# Accept redirect targets (url_name + args), resolved by the ``suggestion_act`` view — the capsuleer
# namespace is mounted, so these reverse to live routes.
_ACCEPT_ROUTE = {
    SuggestionKind.NEAR_QUALIFICATION: ("capsuleer:goal_detail", ["goal"]),
    SuggestionKind.EVENT_MATCH: ("operations:detail", ["operation_id"]),
    SuggestionKind.MENTOR_AVAILABLE: ("mentorship:mentors", []),
    SuggestionKind.STALLED_GOAL: ("capsuleer:goal_review", ["goal"]),
    SuggestionKind.BLOCKED_PREREQ: ("capsuleer:goal_detail", ["goal"]),
    SuggestionKind.SHIP_AVAILABLE: ("store:hull", ["ship_type_id"]),
    SuggestionKind.CAMPAIGN_OPPORTUNITY: ("campaigns:detail", ["campaign_id"]),
    SuggestionKind.REVIEW_DUE: ("capsuleer:goal_review", ["goal"]),
}

_VALID_ACTIONS = {"accept", "dismiss", "defer", "not_interested", "incorrect"}


def _accept_redirect(suggestion) -> dict:
    """A ``{url_name, args}`` redirect hint for the accept action, resolved by the view (doc 08 §5)."""
    route = _ACCEPT_ROUTE.get(suggestion.kind)
    if route is None:
        return {"url_name": "capsuleer:home", "args": []}
    url_name, arg_spec = route
    inputs = (suggestion.data or {}).get("inputs", {})
    args = []
    for spec in arg_spec:
        if spec == "goal":
            args.append(suggestion.goal_id)
        else:
            args.append(inputs.get(spec))
    if suggestion.kind == SuggestionKind.REVIEW_DUE and suggestion.goal_id is None:
        return {"url_name": "capsuleer:profile", "args": []}
    return {"url_name": url_name, "args": args}


def act_on_suggestion(user, suggestion, action, *, now=None) -> dict:
    """Apply an owner action to a suggestion (doc 08 §7). Owner-only — a non-owner is rejected (the
    view layer maps this to 404). Returns ``{status, redirect}`` where ``redirect`` (accept only)
    carries ``url_name`` + ``args`` the view resolves. ``incorrect`` is displayed as dismissed and
    never mutes the kind (that stays with ``not_interested``)."""
    if suggestion.user_id != getattr(user, "pk", None):
        raise ValidationError("That suggestion is not yours.")
    if action not in _VALID_ACTIONS:
        raise ValidationError(f"Unknown action: {action!r}.")
    now = now or timezone.now()

    if action == "accept":
        return _accept(user, suggestion, now)

    status_map = {
        "dismiss": SuggestionStatus.DISMISSED,
        "defer": SuggestionStatus.DEFERRED,
        "not_interested": SuggestionStatus.NOT_INTERESTED,
        "incorrect": SuggestionStatus.INCORRECT,
    }
    suggestion.status = status_map[action]
    suggestion.acted_at = now
    suggestion.save(update_fields=["status", "acted_at", "updated_at"])
    if action == "not_interested":
        mute_kind(user, suggestion.kind, now=now)
    return {"status": suggestion.status, "redirect": None}


def _accept(user, suggestion, now) -> dict:
    with transaction.atomic():
        suggestion.status = SuggestionStatus.ACCEPTED
        suggestion.acted_at = now
        suggestion.save(update_fields=["status", "acted_at", "updated_at"])
        if suggestion.goal_id is not None:
            from . import services

            goal = CareerGoal.objects.filter(pk=suggestion.goal_id, user=user).first()
            # Enforce the single tunable step cap — never a second hardcoded value that drifts from
            # the ``step_add`` guard (finding 53, doc 09 T-23).
            if goal is not None and goal.action_steps.count() < services.MAX_STEPS_PER_GOAL:
                CareerActionStep.objects.create(
                    goal=goal, title=suggestion.title[:140], source=StepSource.SUGGESTION,
                )
                services.record_activity(goal, user, "suggestion.accepted",
                                         {"kind": suggestion.kind})
    return {"status": suggestion.status, "redirect": _accept_redirect(suggestion)}


def mute_kind(user, kind, *, now=None) -> None:
    """Add ``kind`` to the profile's muted list (idempotent) and expire its open rows (doc 08 §9)."""
    now = now or timezone.now()
    profile, _ = CareerProfile.objects.get_or_create(user=user)
    muted = list(profile.suggestion_muted_kinds or [])
    if kind not in muted:
        muted.append(kind)
        profile.suggestion_muted_kinds = muted
        profile.save(update_fields=["suggestion_muted_kinds", "updated_at"])
    PathSuggestion.objects.filter(
        user=user, kind=kind, status=SuggestionStatus.OPEN
    ).update(expires_at=now)


def unmute_kind(user, kind) -> None:
    """Remove ``kind`` from the profile's muted list (profile page control; generation resumes next
    run, nothing backfilled — doc 08 §9)."""
    profile = CareerProfile.objects.filter(user=user).first()
    if profile is None:
        return
    muted = [k for k in (profile.suggestion_muted_kinds or []) if k != kind]
    if muted != (profile.suggestion_muted_kinds or []):
        profile.suggestion_muted_kinds = muted
        profile.save(update_fields=["suggestion_muted_kinds", "updated_at"])
