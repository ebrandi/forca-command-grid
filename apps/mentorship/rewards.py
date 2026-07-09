"""Reward engine & ledger.

Nothing here moves ISK. A reward is *recorded* in ``MentorshipRewardLedger``;
ISK rewards are marked paid by an officer with a free-text reference — identical
to SRP. Points feed the corp contribution ledger; badges/titles are cosmetic.

Guards (mirroring ``apps.srp.services``):
  * **Idempotent** per source action via ``dedupe_key`` (DB unique constraint).
  * **Caps** (per-rule and per-cohort/role ISK caps) and **cooldowns** prevent farming.
  * **Verification gate**: a rule may require the triggering task to be auto-verified.
  * **Separation of duties**: an officer can't approve or pay their own reward
    (superuser is the break-glass exemption), enforced under a row lock.
"""
from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    MentorshipBadgeAward,
    MentorshipFlag,
    MentorshipProgram,
    MentorshipRewardLedger,
    MentorshipRewardRule,
)

_R = MentorshipRewardLedger
_RULE = MentorshipRewardRule
VERIFY_THRESHOLD = 60


# ---------------------------------------------------------------------------
# Trigger entry points (called from workflow / services / beats)
# ---------------------------------------------------------------------------
def on_task_completed(assignment) -> list[MentorshipRewardLedger]:
    task = assignment.task
    rules = _RULE.objects.filter(
        active=True, trigger=_RULE.Trigger.TASK, trigger_ref=task.key
    )
    return _grant_for_rules(
        rules, assignment.pairing, assignment=assignment, trigger_ref=task.key,
        confidence=assignment.confidence, validation_method=task.validation_method,
    )


def on_track_completed(pairing, track) -> list[MentorshipRewardLedger]:
    rules = _RULE.objects.filter(
        active=True, trigger=_RULE.Trigger.TRACK_COMPLETE, trigger_ref=track.key
    )
    return _grant_for_rules(rules, pairing, trigger_ref=track.key, confidence=80)


def on_program_completed(pairing) -> list[MentorshipRewardLedger]:
    rules = _RULE.objects.filter(active=True, trigger=_RULE.Trigger.PROGRAM_COMPLETE)
    return _grant_for_rules(rules, pairing, trigger_ref="program", confidence=80)


def on_session_confirmed(session) -> list[MentorshipRewardLedger]:
    rules = _RULE.objects.filter(active=True, trigger=_RULE.Trigger.SESSION)
    return _grant_for_rules(rules, session.pairing, trigger_ref=f"session:{session.pk}", confidence=60)


def on_pairing_active_days(pairing, days: int) -> list[MentorshipRewardLedger]:
    granted = []
    for rule in _RULE.objects.filter(active=True, trigger=_RULE.Trigger.PAIRING_ACTIVE_DAYS):
        try:
            threshold = int(rule.trigger_ref or 0)
        except ValueError:
            continue
        if threshold and days >= threshold:
            granted += _grant_for_rules([rule], pairing, trigger_ref=str(threshold), confidence=70)
    return granted


def grant_milestone(pairing, milestone_key: str, *, confidence=60) -> list[MentorshipRewardLedger]:
    rules = _RULE.objects.filter(
        active=True, trigger=_RULE.Trigger.MILESTONE, trigger_ref=milestone_key
    )
    return _grant_for_rules(rules, pairing, trigger_ref=milestone_key, confidence=confidence)


# ---------------------------------------------------------------------------
# Granting
# ---------------------------------------------------------------------------
def _recipients(rule, pairing):
    """(user, role) pairs a rule applies to."""
    out = []
    if rule.audience in (_RULE.Audience.MENTEE, _RULE.Audience.BOTH):
        out.append((pairing.mentee.user, _R.Role.MENTEE))
    if rule.audience in (_RULE.Audience.MENTOR, _RULE.Audience.BOTH):
        out.append((pairing.mentor.user, _R.Role.MENTOR))
    return out


def _grant_for_rules(rules, pairing, *, assignment=None, trigger_ref="", confidence=0,
                     validation_method="") -> list[MentorshipRewardLedger]:
    program = _active_program()
    if not program.rewards_enabled:
        return []
    granted: list[MentorshipRewardLedger] = []
    for rule in rules:
        if rule.cohort_id and pairing.cohort_id and rule.cohort_id != pairing.cohort_id:
            continue
        if rule.requires_verification and assignment is not None and confidence < VERIFY_THRESHOLD:
            continue
        for user, role in _recipients(rule, pairing):
            entry = _grant_one(program, rule, user, role, pairing, assignment,
                               trigger_ref, confidence, validation_method)
            if entry is not None:
                granted.append(entry)
    return granted


def _dedupe_key(rule, user, pairing, trigger_ref, assignment) -> str:
    a = f"{assignment.pk}:{assignment.repeat_index}" if assignment is not None else ""
    return f"{rule.key}:{user.id}:{pairing.pk}:{trigger_ref}:{a}"


def _grant_one(program, rule, user, role, pairing, assignment, trigger_ref,
               confidence, validation_method) -> MentorshipRewardLedger | None:
    dedupe = _dedupe_key(rule, user, pairing, trigger_ref, assignment)
    if MentorshipRewardLedger.objects.filter(dedupe_key=dedupe).exists():
        return None

    # Cooldown: don't grant the same rule to the same recipient too often.
    if rule.cooldown_hours:
        from datetime import timedelta
        since = timezone.now() - timedelta(hours=rule.cooldown_hours)
        if MentorshipRewardLedger.objects.filter(
            rule=rule, recipient=user, created_at__gte=since
        ).exclude(status__in=[_R.Status.REJECTED, _R.Status.CANCELLED]).exists():
            return None

    amount = Decimal(rule.amount or 0)
    if rule.reward_type == _RULE.RewardType.ISK and amount > 0:
        amount = _apply_caps(program, rule, user, role, amount, pairing)
        if amount <= 0:
            return None

    status = _initial_status(program, rule)
    entry = MentorshipRewardLedger(
        rule=rule, rule_key=rule.key, recipient=user, recipient_role=role,
        pairing=pairing, assignment=assignment, trigger=rule.trigger, trigger_ref=trigger_ref,
        reward_type=rule.reward_type, amount=amount, points=rule.points, badge=rule.badge,
        title_text=rule.title_text, description=rule.label, validation_method=validation_method,
        confidence=confidence, status=status, dedupe_key=dedupe,
        rule_snapshot={
            "key": rule.key, "label": rule.label, "reward_type": rule.reward_type,
            "amount": str(rule.amount), "points": rule.points,
            "requires_leadership_approval": rule.requires_leadership_approval,
            "requires_verification": rule.requires_verification,
        },
    )
    try:
        entry.save()
    except IntegrityError:
        return None  # lost a race on the dedupe constraint

    # Cosmetic / point rewards settle immediately at their granted status.
    if entry.status == _R.Status.APPROVED:
        _settle(entry)
    return entry


def _initial_status(program, rule) -> str:
    """ISK follows the programme's reward_mode + rule approval flag; points/badges/
    titles auto-approve (low value, no ISK), custom is recorded."""
    if rule.reward_type == _RULE.RewardType.ISK:
        if program.reward_mode == MentorshipProgram.RewardMode.AUTO:
            return _R.Status.APPROVED
        if program.reward_mode == MentorshipProgram.RewardMode.RECORDED_ONLY:
            return _R.Status.ELIGIBLE
        # QUEUED
        return _R.Status.PENDING_APPROVAL if rule.requires_leadership_approval else _R.Status.APPROVED
    if rule.reward_type == _RULE.RewardType.CUSTOM:
        return _R.Status.ELIGIBLE
    return _R.Status.APPROVED  # points / badge / title


def _settle(entry: MentorshipRewardLedger) -> None:
    """Apply the non-ISK side effects of an approved reward (points, badge)."""
    if entry.reward_type == _RULE.RewardType.POINTS and entry.points:
        _credit_points(entry)
    elif entry.reward_type == _RULE.RewardType.BADGE and entry.badge_id:
        MentorshipBadgeAward.objects.get_or_create(
            badge=entry.badge, user=entry.recipient,
            defaults={"pairing": entry.pairing, "reason": entry.description},
        )


def _credit_points(entry: MentorshipRewardLedger) -> None:
    from apps.pilots.models import ContributionEvent
    from apps.pilots.services import record_contribution

    record_contribution(
        entry.recipient, ContributionEvent.Kind.TASK, magnitude=entry.points, unit="points",
        description=f"Mentorship: {entry.description}"[:200],
        ref_type="mentorship_reward", ref_id=str(entry.pk), points=entry.points,
    )


def _apply_caps(program, rule, user, role, amount: Decimal, pairing) -> Decimal:
    """Trim ``amount`` to the smaller of the per-rule and per-cohort/role ISK caps.

    Returns the grantable amount (0 if fully capped out, and raises a CAP_HIT flag).
    """
    remaining = amount
    # Per-rule cap across this recipient.
    if rule.cap_per_recipient and rule.cap_per_recipient > 0:
        used = _isk_total(recipient=user, rule=rule)
        remaining = min(remaining, rule.cap_per_recipient - used)
    # Per-cohort/role programme cap.
    cap = program.mentor_reward_cap_isk if role == _R.Role.MENTOR else program.mentee_reward_cap_isk
    if cap and cap > 0:
        used = _isk_total(recipient=user, role=role, cohort_id=pairing.cohort_id)
        remaining = min(remaining, cap - used)
    if remaining <= 0:
        _flag_cap(pairing, user, rule)
        return Decimal(0)
    return remaining


def _isk_total(*, recipient, rule=None, role=None, cohort_id=None) -> Decimal:
    from django.db.models import Sum

    qs = MentorshipRewardLedger.objects.filter(
        recipient=recipient, reward_type=_RULE.RewardType.ISK,
    ).exclude(status__in=[_R.Status.REJECTED, _R.Status.CANCELLED, _R.Status.EXPIRED])
    if rule is not None:
        qs = qs.filter(rule=rule)
    if role is not None:
        qs = qs.filter(recipient_role=role)
    if cohort_id is not None:
        qs = qs.filter(pairing__cohort_id=cohort_id)
    return qs.aggregate(t=Sum("amount"))["t"] or Decimal(0)


def _flag_cap(pairing, user, rule) -> None:
    MentorshipFlag.objects.get_or_create(
        dedupe_key=f"cap:{user.id}:{rule.key}",
        resolved=False,
        defaults={
            "kind": MentorshipFlag.Kind.CAP_HIT, "severity": 30, "pairing": pairing,
            "user": user, "detail": f"Reward cap reached for rule '{rule.label}'.",
        },
    )


def _active_program():
    from . import services
    return services.active_program()


# ---------------------------------------------------------------------------
# Approval / payment workflow (SoD-guarded)
# ---------------------------------------------------------------------------
@transaction.atomic
def approve_reward(entry: MentorshipRewardLedger, officer) -> bool:
    if entry.recipient_id == officer.id and not officer.is_superuser:
        raise PermissionDenied("You cannot approve your own mentorship reward.")
    locked = MentorshipRewardLedger.objects.select_for_update().get(pk=entry.pk)
    if locked.status not in (_R.Status.PENDING_APPROVAL, _R.Status.ELIGIBLE, _R.Status.PENDING_VALIDATION):
        return False
    locked.status = _R.Status.APPROVED
    locked.approved_by = officer
    locked.approved_at = timezone.now()
    locked.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    _settle(locked)
    return True


@transaction.atomic
def reject_reward(entry: MentorshipRewardLedger, officer, reason="") -> bool:
    if entry.recipient_id == officer.id and not officer.is_superuser:
        raise PermissionDenied("You cannot decide your own mentorship reward.")
    locked = MentorshipRewardLedger.objects.select_for_update().get(pk=entry.pk)
    if locked.status in (_R.Status.PAID, _R.Status.REJECTED, _R.Status.CANCELLED):
        return False
    locked.status = _R.Status.REJECTED
    locked.approved_by = officer
    locked.reason = reason[:300]
    locked.save(update_fields=["status", "approved_by", "reason", "updated_at"])
    return True


@transaction.atomic
def mark_reward_paid(entry: MentorshipRewardLedger, officer, reference="") -> bool:
    if entry.recipient_id == officer.id and not officer.is_superuser:
        raise PermissionDenied("You cannot pay your own mentorship reward.")
    locked = MentorshipRewardLedger.objects.select_for_update().get(pk=entry.pk)
    if locked.status not in (_R.Status.APPROVED, _R.Status.ELIGIBLE):
        return False
    locked.status = _R.Status.PAID
    locked.paid_by = officer
    locked.paid_at = timezone.now()
    locked.payment_reference = reference[:200]
    locked.save(update_fields=["status", "paid_by", "paid_at", "payment_reference", "updated_at"])
    return True


def outstanding_isk() -> Decimal:
    """Open ISK liability: everything owed but not yet paid/rejected."""
    from django.db.models import Sum

    return MentorshipRewardLedger.objects.filter(
        reward_type=_RULE.RewardType.ISK, status__in=MentorshipRewardLedger.OPEN_STATUSES
    ).aggregate(t=Sum("amount"))["t"] or Decimal(0)
