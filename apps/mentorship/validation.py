"""Task auto-validation registry.

A task's ``criteria`` JSON (``{"type": "...", ...}``) is dispatched here to an
auto-check, mirroring onboarding's ``_criterion_met`` but returning a *confidence
score* rather than a bare bool. Each validator observes Command Grid's
**already-synced** ESI / internal tables (skill snapshots, killmails, the mining
ledger, corp industry jobs, courier contracts, the contribution ledger) rather
than making its own live ESI call — so validation is cache-friendly, needs no
extra per-pilot scopes beyond what the corp already syncs, and never hammers ESI
from a web request. Live presence polling (location/fleet, which have no ESI
history) is the one exception and lives in ``tasks.py`` behind an opt-in session.

Confidence bands (see docs/design/mentorship-program.md §ESI Feasibility Matrix):
  85–95  strongly verifiable (skills, shared killmail, verified courier)
  65–80  partially verifiable (personal killmails, mining, industry, sessions)
  40–60  weak signals (skill-queue intent, total-SP, scopes granted)

Every time-windowed check is bounded to the **pairing window** (activity must
have happened after the pairing started), so a mentee can't claim pre-mentorship
history — a core anti-abuse property.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.utils import timezone

from . import messages as msg


@dataclass(frozen=True)
class Outcome:
    """Result of an auto-check. ``partial`` means the check ran but couldn't reach
    a confident verdict (missing scope, no synced data yet) — never a hard fail.

    Seam B: ``detail`` is *persisted* (``MentorshipTaskValidation.detail`` and
    ``MentorshipTaskAssignment.last_reason``) and read back by the mentee, the mentor and officers
    under their own locales — and the ``mentorship.sweep_api_validations`` worker that writes most
    of them has no locale at all. So every outcome also carries the scaffold ``key`` + JSON-safe
    ``params`` it was built from; ``detail`` is the English rendering of exactly that pair.
    Build one with :func:`_out` rather than by hand, so the two can never drift.
    """

    passed: bool
    confidence: int = 0
    detail: str = ""
    partial: bool = False
    key: str = ""
    params: dict | None = None


def _out(passed: bool, confidence: int, key: str, params: dict | None = None,
         partial: bool = False) -> Outcome:
    """An Outcome whose English ``detail`` is derived from the scaffold ``key`` + ``params``."""
    params = params or {}
    return Outcome(
        passed=passed, confidence=confidence, detail=msg.english(key, params),
        partial=partial, key=key, params=params,
    )


VALIDATORS: dict[str, callable] = {}


def register(type_key: str):
    def deco(fn):
        VALIDATORS[type_key] = fn
        return fn

    return deco


def auto_types() -> set[str]:
    return set(VALIDATORS)


def is_auto_criteria(criteria: dict | None) -> bool:
    return bool(criteria) and criteria.get("type") in VALIDATORS


def run(assignment, criteria: dict | None = None) -> Outcome | None:
    """Run the criteria's auto-check. Returns None when there is no auto-check
    (manual task). Never raises — a broken/absent data source yields a partial."""
    criteria = criteria if criteria is not None else (assignment.task.criteria or {})
    fn = VALIDATORS.get((criteria or {}).get("type"))
    if fn is None:
        return None
    try:
        return fn(assignment, criteria)
    except Exception as exc:  # noqa: BLE001 - validation must never break the flow
        import logging

        logging.getLogger("forca.mentorship").warning(
            "validator %s failed: %s", criteria.get("type"), exc
        )
        return _out(False, 0, "check.unavailable", partial=True)


# --- shared helpers ---------------------------------------------------------
def _mentee_char_ids(assignment) -> list[int]:
    user = assignment.pairing.mentee.user
    return list(user.characters.values_list("character_id", flat=True))


def _mentor_char_ids(assignment) -> list[int]:
    user = assignment.pairing.mentor.user
    return list(user.characters.values_list("character_id", flat=True))


def _window_start(assignment, criteria: dict):
    """Lower time bound: the later of the pairing start and now-`days`."""
    p = assignment.pairing
    start = p.started_at or p.created_at
    days = criteria.get("days")
    if days:
        floor = timezone.now() - timedelta(days=int(days))
        if start is None or floor > start:
            return floor
    return start


def _latest_snapshot(char_ids):
    from apps.characters.models import CharacterSkillSnapshot

    return (
        CharacterSkillSnapshot.objects.filter(character_id__in=char_ids, is_latest=True)
        .order_by("-total_sp")
        .first()
    )


# --- skill / doctrine (strong; default login scopes) ------------------------
@register("skill_min")
def _skill_min(assignment, criteria):
    snap = _latest_snapshot(_mentee_char_ids(assignment))
    if snap is None:
        return _out(False, 0, "check.no_skills", partial=True)
    want = int(criteria.get("level", 1))
    have = snap.trained_level(int(criteria["skill_type_id"]))
    ok = have >= want
    return _out(ok, 85 if ok else 0, "check.skill_min", {"have": have, "want": want})


@register("total_sp_min")
def _total_sp_min(assignment, criteria):
    snap = _latest_snapshot(_mentee_char_ids(assignment))
    if snap is None:
        return _out(False, 0, "check.no_skills", partial=True)
    want = int(criteria.get("sp", 0))
    ok = (snap.total_sp or 0) >= want
    # SP can be injected, so this is a moderate signal only. The thousands separators are baked into
    # the params (plain strings): a param is substituted raw and is never re-formatted per locale.
    return _out(ok, 60 if ok else 0, "check.total_sp_min",
                {"total": f"{snap.total_sp:,}", "need": f"{want:,}"})


@register("skillqueue_has")
def _skillqueue_has(assignment, criteria):
    from apps.characters.models import SkillQueueSnapshot

    want = int(criteria["skill_type_id"])
    for snap in SkillQueueSnapshot.objects.filter(
        character_id__in=_mentee_char_ids(assignment), is_latest=True
    ):
        for entry in snap.entries or []:
            if int(entry.get("skill_id", entry.get("skill_type_id", 0))) == want:
                return _out(True, 55, "check.skillqueue_present")
    return _out(False, 0, "check.skillqueue_absent", partial=True)


@register("doctrine_ready")
def _doctrine_ready(assignment, criteria):
    from apps.doctrines.services import flyable_doctrine_ids

    snap = _latest_snapshot(_mentee_char_ids(assignment))
    if snap is None:
        return _out(False, 0, "check.no_skills", partial=True)
    want = int(criteria["doctrine_id"])
    ok = want in flyable_doctrine_ids(snap.skills or {})
    return _out(ok, 85 if ok else 0,
                "check.doctrine_ready" if ok else "check.doctrine_not_ready")


@register("doctrine_any")
def _doctrine_any(assignment, criteria):
    from apps.doctrines.services import flyable_doctrine_ids

    snap = _latest_snapshot(_mentee_char_ids(assignment))
    if snap is None:
        return _out(False, 0, "check.no_skills", partial=True)
    flyable = flyable_doctrine_ids(snap.skills or {})
    ok = bool(flyable)
    return _out(ok, 80 if ok else 0, "check.doctrine_any", {"count": len(flyable)})


@register("skill_plan_exists")
def _skill_plan_exists(assignment, criteria):
    from apps.skills.models import SkillPlan

    qs = SkillPlan.objects.filter(character_id__in=_mentee_char_ids(assignment))
    if criteria.get("doctrine_id"):
        qs = qs.filter(target_doctrine_id=int(criteria["doctrine_id"]))
    ok = qs.exists()
    return _out(ok, 70 if ok else 0,
                "check.skill_plan_exists" if ok else "check.skill_plan_missing", partial=not ok)


# --- PvP / killmails (shared participation = strong) ------------------------
@register("killmail_recent")
def _killmail_recent(assignment, criteria):
    from apps.killboard.models import KillmailParticipant

    since = _window_start(assignment, criteria)
    role = criteria.get("role", "attacker")
    qs = KillmailParticipant.objects.filter(character_id__in=_mentee_char_ids(assignment), role=role)
    if since:
        qs = qs.filter(killmail__killmail_time__gte=since)
    count = qs.values("killmail_id").distinct().count()
    need = int(criteria.get("min_count", 1))
    ok = count >= need
    return _out(ok, 70 if ok else 0, "check.killmail_recent",
                {"count": count, "need": need}, partial=not ok)


@register("shared_killmail")
def _shared_killmail(assignment, criteria):
    """Mentor AND mentee both on the same killmail during the pairing — durable
    proof they flew together (ESI killmail bodies are immutable)."""
    from apps.killboard.models import Killmail

    since = _window_start(assignment, criteria)
    mentee_ids = _mentee_char_ids(assignment)
    mentor_ids = _mentor_char_ids(assignment)
    qs = Killmail.objects.filter(participants__character_id__in=mentee_ids).filter(
        participants__character_id__in=mentor_ids
    )
    if since:
        qs = qs.filter(killmail_time__gte=since)
    ok = qs.distinct().exists()
    return _out(ok, 90 if ok else 0,
                "check.shared_killmail" if ok else "check.shared_killmail_none", partial=not ok)


# --- internal-event validators (strong; already on the contribution bus) ----
@register("fleet_attended")
def _fleet_attended(assignment, criteria):
    from apps.pilots.models import ContributionEvent

    since = _window_start(assignment, criteria)
    qs = ContributionEvent.objects.filter(
        user=assignment.pairing.mentee.user, kind=ContributionEvent.Kind.FLEET
    )
    if since:
        qs = qs.filter(occurred_at__gte=since)
    count = qs.count()
    need = int(criteria.get("min_count", 1))
    ok = count >= need
    return _out(ok, 80 if ok else 0, "check.fleet_attended",
                {"count": count, "need": need}, partial=not ok)


@register("contribution_kind")
def _contribution_kind(assignment, criteria):
    from apps.pilots.models import ContributionEvent

    since = _window_start(assignment, criteria)
    qs = ContributionEvent.objects.filter(
        user=assignment.pairing.mentee.user, kind=criteria["kind"]
    )
    if criteria.get("ref_type"):
        qs = qs.filter(ref_type=criteria["ref_type"])
    if since:
        qs = qs.filter(occurred_at__gte=since)
    count = qs.count()
    need = int(criteria.get("min_count", 1))
    ok = count >= need
    # ``kind`` is a ContributionEvent.Kind code — an identifier, interpolated raw.
    return _out(ok, 75 if ok else 0, "check.contribution_kind",
                {"count": count, "kind": criteria["kind"]}, partial=not ok)


@register("courier_contract")
def _courier_contract(assignment, criteria):
    from apps.logistics.models import CourierContract

    since = _window_start(assignment, criteria)
    qs = CourierContract.objects.filter(
        assigned_user=assignment.pairing.mentee.user,
        status=CourierContract.Status.DELIVERED,
    )
    if criteria.get("verified_only", True):
        qs = qs.filter(verification_state="verified")
    if since:
        qs = qs.filter(updated_at__gte=since)
    ok = qs.exists()
    conf = 85 if criteria.get("verified_only", True) else 70
    return _out(ok, conf if ok else 0,
                "check.courier_delivered" if ok else "check.courier_none", partial=not ok)


@register("buyback_offer")
def _buyback_offer(assignment, criteria):
    from apps.buyback.models import BuybackOffer

    since = _window_start(assignment, criteria)
    qs = BuybackOffer.objects.filter(seller=assignment.pairing.mentee.user)
    if criteria.get("status"):
        qs = qs.filter(status=criteria["status"])
    if since:
        qs = qs.filter(created_at__gte=since)
    ok = qs.exists()
    return _out(ok, 75 if ok else 0,
                "check.buyback_submitted" if ok else "check.buyback_none", partial=not ok)


# --- mining / industry (need corp observer / job coverage) ------------------
@register("mining_ledger")
def _mining_ledger(assignment, criteria):
    from django.db.models import Sum

    from apps.mining.models import MiningLedgerEntry

    since = _window_start(assignment, criteria)
    qs = MiningLedgerEntry.objects.filter(character_id__in=_mentee_char_ids(assignment))
    if since:
        qs = qs.filter(day__gte=since.date())
    total = qs.aggregate(q=Sum("quantity"))["q"] or 0
    need = int(criteria.get("min_units", 1))
    ok = total >= need
    # Corp mining ledger only sees mining at corp observers → partial by nature.
    return _out(ok, 65 if ok else 0, "check.mining_ledger",
                {"total": f"{total:,}", "need": f"{need:,}"}, partial=not ok)


@register("industry_job")
def _industry_job(assignment, criteria):
    from apps.erp.models import CorpIndustryJob

    since = _window_start(assignment, criteria)
    qs = CorpIndustryJob.objects.filter(installer_id__in=_mentee_char_ids(assignment))
    if criteria.get("activity_id"):
        qs = qs.filter(activity_id=int(criteria["activity_id"]))
    if since:
        qs = qs.filter(start_date__gte=since)
    ok = qs.exists()
    return _out(ok, 75 if ok else 0,
                "check.industry_installed" if ok else "check.industry_none", partial=not ok)


# --- sessions & scopes ------------------------------------------------------
@register("session_confirmed")
def _session_confirmed(assignment, criteria):
    from .models import MentorshipSession

    need = int(criteria.get("min_participants", 2))
    for session in assignment.pairing.sessions.filter(
        status=MentorshipSession.Status.COMPLETED
    ).prefetch_related("participants"):
        confirmed = sum(1 for p in session.participants.all() if p.confirmed)
        if confirmed >= need:
            return _out(True, 65, "check.session_confirmed")
    return _out(False, 0, "check.session_none", partial=True)


@register("scopes_granted")
def _scopes_granted(assignment, criteria):
    from apps.sso.models import AuthToken

    want = set(criteria.get("scopes", []))
    if not want:
        return _out(True, 50, "check.scopes_none_required")
    for token in AuthToken.objects.filter(
        character_id__in=_mentee_char_ids(assignment), revoked_at__isnull=True
    ):
        if want.issubset(set(token.scopes or [])):
            return _out(True, 70, "check.scopes_granted")
    return _out(False, 0, "check.scopes_missing", partial=True)
