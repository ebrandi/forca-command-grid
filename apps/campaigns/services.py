"""Campaign Command business logic — the only place campaign state changes.

Views and tasks are thin: they parse input, call a service, and render. Every rule that matters
lives here — the guarded 9-state lifecycle (doc 04 §1, T1–T11), the visibility chokepoint and
object re-check (doc 07 §1.4), objective measurement/verification, weighted/milestone/manual
progress, deterministic health, dependency acyclicity + auto-resolution, and the issue↔objective
block round-trip. All mutations take the campaign row lock (`select_for_update`) before recomputing
progress/health, so a concurrent officer edit and a background refresh converge instead of
clobbering (doc 05 §5). Progress arithmetic is delegated to the pure :mod:`.progress` module and
health scoring to the pure :mod:`.health` module so both are unit-testable without a database.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify

from core import freshness
from core.audit import audit_log
from core.rbac import (
    PERM_CAMPAIGN_MANAGE,
    ROLE_DIRECTOR,
    ROLE_OFFICER,
    ROLE_RANK,
    effective_rank,
    has_perm,
    has_role,
)

from . import calendar as cal
from . import config, health, metrics, notify, progress
from .models import (
    Campaign,
    CampaignActivity,
    CampaignDependency,
    CampaignRecognition,
    CampaignTemplate,
    DependencyKind,
    Issue,
    MeasurementSource,
    Milestone,
    Objective,
    ObjectiveSample,
    Risk,
    Workstream,
)
from .templates_builtin import INSTANCE_PARAM_KEYS

logger = logging.getLogger("forca.campaigns")

# Dependency-graph safety: a creation-time walk confined to one campaign rejects cycles and
# caps chain depth (the DoS guard of doc 07 T20). Campaign-scoped and unique-per-pair, the graph
# is bounded; this cap only ever bites a pathological construction.
_DEPENDENCY_DEPTH_CAP = 50

# Probability/impact → weight; severity = P × I in 1..9 (doc 06 §4.13). The mapping lives here,
# never in the enum, and is recomputed on every risk write so it can't be mass-assigned.
_RISK_WEIGHT = {Risk.RiskLevel.LOW: 1, Risk.RiskLevel.MEDIUM: 2, Risk.RiskLevel.HIGH: 3}


# --------------------------------------------------------------------------- #
#  Permissions & visibility (doc 07)
# --------------------------------------------------------------------------- #
def visible_campaigns(user):
    """The single queryset chokepoint every list/panel/fragment enumerates through (doc 07 §1.4).

    Directors see all. Everyone else sees campaigns whose visibility tier admits them **and** which
    are past the pre-approval phase, plus — at any status/tier — campaigns they personally run
    (commander/sponsor/creator/workstream lead/objective owner), the per-object widening rule that
    lets a participant do the work a bare tier would hide. Draft/proposed campaigns are never
    visible by tier alone (doc 04 §1).
    """
    if not getattr(user, "is_authenticated", False):
        return Campaign.objects.none()
    if effective_rank(user) >= ROLE_RANK[ROLE_DIRECTOR]:
        return Campaign.objects.all()

    status = Campaign.Status
    vis = Campaign.Visibility
    tier = Q(visibility=vis.MEMBERS)
    if effective_rank(user) >= ROLE_RANK[ROLE_OFFICER]:
        tier |= Q(visibility=vis.OFFICERS)
    tier |= Q(visibility=vis.RESTRICTED, restricted_users=user)
    tier_visible = tier & ~Q(status__in=[status.DRAFT, status.PROPOSED])
    personal = (
        Q(commander=user) | Q(sponsor=user) | Q(created_by=user)
        | Q(workstreams__lead=user) | Q(objectives__owner=user)
    )
    return Campaign.objects.filter(tier_visible | personal).distinct()


def can_view(user, campaign) -> bool:
    """Object-level re-check (never trust the list filter alone — IDOR defence, doc 07 §1.4)."""
    if not getattr(user, "is_authenticated", False):
        return False
    return visible_campaigns(user).filter(pk=campaign.pk).exists()


def can_manage(user, campaign) -> bool:
    """Full campaign management: the manage capability (officer baseline or ``campaign_lead``
    lateral grant) or the campaign's commander (doc 07 §1.1)."""
    if not getattr(user, "is_authenticated", False):
        return False
    if has_perm(user, PERM_CAMPAIGN_MANAGE):
        return True
    return campaign.commander_id == user.pk


def can_update_objective(user, objective) -> bool:
    """Progress/evidence rights on one objective: manage, the objective owner, or the owning
    workstream's lead (doc 07 §1.1)."""
    uid = getattr(user, "pk", None)
    if uid is None:
        return False
    if can_manage(user, objective.campaign):
        return True
    if objective.owner_id == uid:
        return True
    ws = objective.workstream
    return bool(ws and ws.lead_id == uid)


def can_view_budget(user, campaign) -> bool:
    """Budget fields are director + commander only (doc 07 §1.5)."""
    uid = getattr(user, "pk", None)
    if uid is None:
        return False
    return has_role(user, ROLE_DIRECTOR) or campaign.commander_id == uid


def can_view_objective_value(user, objective) -> bool:
    """Sensitive objective measurements (current/baseline/target, samples, sparkline) share the
    budget audience — directors + the campaign commander (doc 07 §1.5). A non-sensitive
    objective's value is visible to anyone who may view the campaign. Read paths call this before
    exposing a value so a sensitive figure is never rendered to an unprivileged viewer."""
    if not objective.is_sensitive:
        return True
    return can_view_budget(user, objective.campaign)


def can_approve(user) -> bool:
    """``proposed → approved`` is director-only, regardless of any other role (doc 07 §1.1)."""
    return has_role(user, ROLE_DIRECTOR)


def can_verify(user, objective) -> bool:
    """Verification needs officer rank **and** verifier ≠ the pilot who claimed the value —
    separation of duties (doc 07 §1.1). Commander/manage rank alone does not verify."""
    if not has_role(user, ROLE_OFFICER):
        return False
    claimant = _met_claimant(objective)
    return claimant is None or claimant.pk != getattr(user, "pk", None)


def can_attach_evidence(user, campaign, attached_kind, attached_id) -> bool:
    """Who may attach/remove evidence on an object (doc 04 §9): the attached entity's owner, its
    workstream lead, or manage capability — never an arbitrary participant. Campaign-level
    evidence is manage-only; objective/milestone evidence widens to their owner/lead."""
    from .models import EvidenceKind

    if can_manage(user, campaign):
        return True
    uid = getattr(user, "pk", None)
    if uid is None:
        return False
    if attached_kind == EvidenceKind.OBJECTIVE:
        obj = campaign.objectives.filter(pk=attached_id).select_related("workstream").first()
        return bool(obj and can_update_objective(user, obj))
    if attached_kind == EvidenceKind.MILESTONE:
        ms = campaign.milestones.filter(pk=attached_id).select_related("workstream").first()
        if not ms:
            return False
        lead_id = ms.workstream.lead_id if ms.workstream_id else None
        return ms.owner_id == uid or lead_id == uid
    return False  # campaign-level evidence handled by the can_manage short-circuit above


def workspace_access(user) -> bool:
    """Officer workspace gate (doc 10 §6.7): manage capability on ≥1 campaign, or the pilot owns
    objectives / leads workstreams / commands a campaign — the simplest correct 'has campaign
    work of my own' test."""
    if not getattr(user, "is_authenticated", False):
        return False
    if has_perm(user, PERM_CAMPAIGN_MANAGE):
        return True
    uid = user.pk
    return (
        Objective.objects.filter(owner_id=uid).exists()
        or Campaign.objects.filter(commander_id=uid).exists()
        or Campaign.objects.filter(workstreams__lead_id=uid).exists()
    )


# --------------------------------------------------------------------------- #
#  Activity stream
# --------------------------------------------------------------------------- #
def record_activity(
    campaign,
    actor,
    verb,
    target_kind="",
    target_id="",
    before=None,
    after=None,
    reason="",
    source="manual",
):
    """Append one row to a campaign's in-page activity stream. Automation passes ``actor=None``
    (stored as NULL) so a human action is never confused with a job."""
    return CampaignActivity.objects.create(
        campaign=campaign,
        actor=actor if getattr(actor, "pk", None) else None,
        verb=str(verb)[:64],
        target_kind=str(target_kind)[:16] if target_kind else "",
        target_id=int(target_id) if target_id else 0,
        before=before,
        after=after,
        reason=(reason or "")[:300],
        source=source,
    )


# --------------------------------------------------------------------------- #
#  Lifecycle (doc 04 §1)
# --------------------------------------------------------------------------- #
# Legal edges only; guards (director for approval, manage otherwise) and per-edge validations are
# enforced below. Anything absent here is rejected — the DB never sees the transition rules.
_LEGAL_TRANSITIONS = {
    (Campaign.Status.DRAFT, Campaign.Status.PROPOSED),
    (Campaign.Status.DRAFT, Campaign.Status.CANCELLED),
    (Campaign.Status.PROPOSED, Campaign.Status.DRAFT),
    (Campaign.Status.PROPOSED, Campaign.Status.APPROVED),
    (Campaign.Status.PROPOSED, Campaign.Status.CANCELLED),
    (Campaign.Status.APPROVED, Campaign.Status.ACTIVE),
    (Campaign.Status.ACTIVE, Campaign.Status.PAUSED),
    (Campaign.Status.PAUSED, Campaign.Status.ACTIVE),
    (Campaign.Status.ACTIVE, Campaign.Status.COMPLETED),
    (Campaign.Status.ACTIVE, Campaign.Status.FAILED),
    (Campaign.Status.ACTIVE, Campaign.Status.CANCELLED),
    (Campaign.Status.COMPLETED, Campaign.Status.ARCHIVED),
    (Campaign.Status.FAILED, Campaign.Status.ARCHIVED),
    (Campaign.Status.CANCELLED, Campaign.Status.ARCHIVED),
}

# Edges whose target status demands a mandatory reason (doc 04 T2/T5/T8/T9/T10).
_REASON_REQUIRED = {
    (Campaign.Status.PROPOSED, Campaign.Status.DRAFT),
    (Campaign.Status.ACTIVE, Campaign.Status.PAUSED),
    (Campaign.Status.ACTIVE, Campaign.Status.FAILED),
    (Campaign.Status.ACTIVE, Campaign.Status.CANCELLED),
    (Campaign.Status.DRAFT, Campaign.Status.CANCELLED),
    (Campaign.Status.PROPOSED, Campaign.Status.CANCELLED),
}

_DEDICATED_AUDIT = {
    Campaign.Status.APPROVED: "campaigns.approved",
    Campaign.Status.COMPLETED: "campaigns.completed",
    Campaign.Status.CANCELLED: "campaigns.cancelled",
    Campaign.Status.ARCHIVED: "campaigns.archived",
}


def can_transition(campaign, to_status, user) -> bool:
    """True if ``user`` may move ``campaign`` to ``to_status`` from its current status."""
    edge = (campaign.status, to_status)
    if edge not in _LEGAL_TRANSITIONS:
        return False
    if edge == (Campaign.Status.PROPOSED, Campaign.Status.APPROVED):
        return can_approve(user)
    return can_manage(user, campaign)


@transaction.atomic
def set_status(campaign, to_status, user, reason="", *, via_closeout=False) -> bool:
    """Guarded lifecycle transition (doc 04 §1): validate under the row lock, write the status,
    log an activity row + audit, then recompute health — all in one transaction.

    Re-posting the current status is an idempotent no-op (doc 07 T14). An illegal or invalid
    transition raises :class:`ValidationError`; because the status is re-read under
    ``select_for_update``, a stale form loses cleanly against an intervening change (doc 07 T13).

    ``active → completed|failed`` is reachable **only** through the guided close-out (doc 04 T7/T8):
    the permanent record (outcome/lessons/objective reconciliation) is mandatory, so a direct edge
    is rejected unless ``via_closeout=True`` (which :func:`close_campaign` passes).
    """
    status = Campaign.Status
    locked = Campaign.objects.select_for_update().get(pk=campaign.pk)
    if locked.status == to_status:
        return True
    if not can_transition(locked, to_status, user):
        raise ValidationError("This status change is not allowed from the campaign's current state.")

    edge = (locked.status, to_status)
    if not via_closeout and edge in (
        (status.ACTIVE, status.COMPLETED), (status.ACTIVE, status.FAILED)
    ):
        raise ValidationError(
            "Complete or fail a campaign through the close-out flow, not a direct status change."
        )
    now = timezone.now()
    if edge in _REASON_REQUIRED and not (reason or "").strip():
        raise ValidationError("A reason is required for this status change.")
    if edge == (status.DRAFT, status.PROPOSED):
        _validate_propose(locked)

    override_reason = ""
    if edge == (status.ACTIVE, status.COMPLETED):
        blockers = _completion_blockers(locked)
        if blockers:
            if not (has_role(user, ROLE_DIRECTOR) and (reason or "").strip()):
                raise ValidationError(
                    "All mandatory objectives must be resolved and verified before completing, "
                    "or a director must supply an override reason."
                )
            override_reason = reason

    started_now = False
    if edge == (status.APPROVED, status.ACTIVE):
        if locked.start_at is None:
            locked.start_at = now
            started_now = True
        if locked.target_end_at and locked.target_end_at <= now:
            raise ValidationError("The target end date must be in the future to start the campaign.")

    old = locked.status
    locked.status = to_status
    update_fields = ["status", "updated_at"]
    if started_now:
        update_fields.append("start_at")
    if to_status in (status.COMPLETED, status.FAILED, status.CANCELLED):
        locked.actual_end_at = now
        locked.closed_by = user if getattr(user, "pk", None) else None
        locked.closed_at = now
        update_fields += ["actual_end_at", "closed_by", "closed_at"]
    locked.save(update_fields=update_fields)

    effective_reason = override_reason or reason
    record_activity(
        locked, user, "status.changed", target_kind="campaign", target_id=locked.pk,
        before={"status": old}, after={"status": to_status}, reason=effective_reason,
    )
    audit_log(
        user, "campaigns.status_changed", target_type="campaign", target_id=str(locked.pk),
        metadata={"from": old, "to": to_status, "reason": effective_reason},
    )
    if to_status in _DEDICATED_AUDIT:
        meta = {"from": old, "reason": effective_reason}
        if override_reason:
            meta["override"] = True
        audit_log(
            user, _DEDICATED_AUDIT[to_status], target_type="campaign",
            target_id=str(locked.pk), metadata=meta,
        )

    if to_status == status.COMPLETED:
        resolve_dependencies_for(DependencyKind.CAMPAIGN, locked.pk)

    _recompute_locked(locked)
    _mirror(campaign, locked)
    _schedule_status_effects(campaign, old, to_status)
    return True


def _schedule_status_effects(campaign, from_status, to_status) -> None:
    """Register the post-commit notification + calendar effects of a lifecycle transition
    (doc 04 §1, doc 09 §4). Emitted after commit so a rollback carries the effect with it, and
    fail-soft inside ``notify``/``calendar`` so neither can break the transition. Health-change
    notification always runs (it self-dedups on an unchanged signature); the lifecycle event and
    calendar effect depend on the target status. A ``proposed → draft`` rework has no registry
    event (doc 09 defines no rework key) and deliberately emits nothing but the health recheck."""
    status = Campaign.Status
    transaction.on_commit(lambda: notify.health_changed(campaign))
    if to_status == status.PROPOSED:
        transaction.on_commit(lambda: notify.approval_needed(campaign))
    elif to_status == status.APPROVED:
        transaction.on_commit(lambda: notify.approved(campaign))
    elif to_status == status.ACTIVE:
        if from_status == status.APPROVED:
            transaction.on_commit(lambda: notify.started(campaign))
        transaction.on_commit(lambda: cal.publish_campaign(campaign))
    elif to_status in (status.COMPLETED, status.FAILED, status.CANCELLED):
        transaction.on_commit(lambda t=to_status: notify.completed(campaign, t))
        transaction.on_commit(lambda: cal.cancel_campaign(campaign))
    elif to_status == status.ARCHIVED:
        transaction.on_commit(lambda: cal.cancel_campaign(campaign))


def _validate_propose(campaign) -> None:
    """T1 minimum-content gate: a proposal needs an outcome and at least one live objective."""
    if not (campaign.desired_outcome or "").strip():
        raise ValidationError("Set the desired outcome before proposing the campaign.")
    if campaign.objectives.exclude(status=Objective.ObjectiveStatus.DROPPED).count() < 1:
        raise ValidationError("Add at least one objective before proposing the campaign.")
    if (campaign.target_end_at and campaign.start_at
            and campaign.target_end_at < campaign.start_at):
        raise ValidationError("The target end date cannot be before the start date.")


def _completion_blockers(campaign) -> list[str]:
    """Mandatory objectives that are not yet terminal, or met-but-unverified (doc 04 T7)."""
    obj_status = Objective.ObjectiveStatus
    terminal = {obj_status.MET, obj_status.MISSED, obj_status.DROPPED}
    blockers = []
    for obj in campaign.objectives.filter(is_mandatory=True):
        if obj.status not in terminal:
            blockers.append(f"objective {obj.pk} unresolved")
        elif (obj.status == obj_status.MET and obj.requires_verification
              and obj.verified_by_id is None):
            blockers.append(f"objective {obj.pk} unverified")
    return blockers


def _mirror(target, source) -> None:
    """Copy the recomputed columns back onto the caller's instance after a locked write."""
    for field in ("status", "start_at", "actual_end_at", "closed_by_id", "closed_at",
                  "progress_pct", "health", "health_reasons"):
        setattr(target, field, getattr(source, field))


# --------------------------------------------------------------------------- #
#  Objective measurement & status (doc 04 §2, §3)
# --------------------------------------------------------------------------- #
@transaction.atomic
def update_manual_value(objective, user, value, note):
    """Record an officer-entered value for an objective (doc 04 §3.2).

    A non-empty ``note`` is mandatory (provenance). On an auto-sourced objective this is a
    correction that the next successful refresh supersedes, and it is audited
    (``campaigns.manual_metric_override``). A same-value, same-minute replay writes no new sample
    (doc 07 T14).
    """
    if not (note or "").strip():
        raise ValidationError("A note is required when entering a value manually.")
    try:
        value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError("The value must be a number.") from exc

    campaign = objective.campaign
    now = timezone.now()
    # Take the campaign row lock first, then re-fetch the objective under it and re-validate —
    # matching the refresh path's lock order (campaign → objective) so a concurrent transition or
    # background refresh can't be clobbered, and a same-minute double-click can't slip a duplicate
    # sample past the replay guard (which now reads the locked row) (#24, #29).
    locked_campaign = Campaign.objects.select_for_update().get(pk=campaign.pk)
    objective = locked_campaign.objectives.select_for_update().get(pk=objective.pk)
    was_auto = bool(objective.metric_source)
    before = {"current_value": _activity_value(objective, objective.current_value)}

    last = objective.samples.order_by("-measured_at", "-id").first()
    same_replay = bool(
        last and last.value == value
        and last.measured_at.replace(second=0, microsecond=0) == now.replace(second=0, microsecond=0)
    )

    objective.current_value = value
    objective.measured_at = now
    objective.measurement_source = MeasurementSource.MANUAL
    objective.last_manual_value_by = user if getattr(user, "pk", None) else None
    objective.last_manual_value_at = now
    objective.manual_note = note
    objective.save(update_fields=[
        "current_value", "measured_at", "measurement_source", "last_manual_value_by",
        "last_manual_value_at", "manual_note", "updated_at",
    ])
    if not same_replay:
        ObjectiveSample.objects.create(
            objective=objective, value=value, measured_at=now,
            source=MeasurementSource.MANUAL, note=(note or "")[:200],
        )
    # The activity ``reason`` renders to every campaign viewer, but a sensitive objective's note
    # shares the value's restricted audience — keep it out of the feed (it survives, gated, on the
    # sample/manual_note surfaces and in the director-only audit, #6). The before/after figures are
    # redacted for a sensitive objective for the same reason (doc 07 T10, #30).
    activity_reason = "" if objective.is_sensitive else (note or "")[:300]
    record_activity(
        campaign, user, "objective.progress", target_kind="objective", target_id=objective.pk,
        before=before, after={"current_value": _activity_value(objective, value)}, reason=activity_reason,
    )
    if was_auto:
        audit_log(
            user, "campaigns.manual_metric_override", target_type="campaign_objective",
            target_id=str(objective.pk),
            metadata={"campaign_id": campaign.pk, "note": (note or "")[:300]},
        )
    _recompute_locked(locked_campaign)
    _mirror(campaign, locked_campaign)
    transaction.on_commit(lambda: notify.health_changed(campaign))
    return objective


# Legal manual objective transitions (blocked is set only by the issue-linkage rule, doc 04 §2).
_OBJECTIVE_TRANSITIONS = {
    Objective.ObjectiveStatus.PENDING: {
        Objective.ObjectiveStatus.ACTIVE, Objective.ObjectiveStatus.MET,
        Objective.ObjectiveStatus.MISSED, Objective.ObjectiveStatus.DROPPED,
    },
    Objective.ObjectiveStatus.ACTIVE: {
        Objective.ObjectiveStatus.MET, Objective.ObjectiveStatus.MISSED,
        Objective.ObjectiveStatus.DROPPED, Objective.ObjectiveStatus.PENDING,
    },
    Objective.ObjectiveStatus.MET: {
        Objective.ObjectiveStatus.ACTIVE, Objective.ObjectiveStatus.MISSED,
        Objective.ObjectiveStatus.DROPPED,
    },
    Objective.ObjectiveStatus.MISSED: {
        Objective.ObjectiveStatus.ACTIVE, Objective.ObjectiveStatus.MET,
        Objective.ObjectiveStatus.DROPPED,
    },
    Objective.ObjectiveStatus.DROPPED: {
        Objective.ObjectiveStatus.ACTIVE, Objective.ObjectiveStatus.PENDING,
    },
}


@transaction.atomic
def set_objective_status(objective, user, to_status, reason="", note=""):
    """Move an objective through its status set (doc 04 §2).

    ``blocked`` is reachable only through a linked issue, so it is rejected here in both
    directions. A ``met`` claim by a manage-capability holder is self-verifying and counts toward
    the completion gate immediately; a claim by anyone else enters ``met`` flagged unverified (it
    clears any prior sign-off) and does not count until an officer who is not the claimant verifies
    it (doc 04 §2). Reaching ``met`` or ``dropped`` auto-resolves dependency edges targeting the
    objective (doc 04 §8).
    """
    obj_status = Objective.ObjectiveStatus
    if to_status == obj_status.BLOCKED:
        raise ValidationError("Objectives become blocked only through a linked issue.")
    if to_status == obj_status.DROPPED and not (reason or "").strip():
        raise ValidationError("A reason is required to drop an objective.")

    campaign = objective.campaign
    # Lock the campaign row first, then re-fetch + re-validate the objective under it, so a
    # concurrent post can't record an illegal edge (e.g. MET→PENDING) or stamp a no-longer-met
    # objective — a compare-and-set matching set_status (#24, #29).
    locked_campaign = Campaign.objects.select_for_update().get(pk=campaign.pk)
    objective = locked_campaign.objectives.select_for_update().get(pk=objective.pk)
    if objective.status == obj_status.BLOCKED:
        raise ValidationError("Resolve the blocking issue before changing this objective's status.")
    if to_status == objective.status:
        return objective
    if to_status not in _OBJECTIVE_TRANSITIONS.get(objective.status, set()):
        raise ValidationError("That objective status change is not allowed.")

    old = objective.status
    objective.status = to_status
    update_fields = ["status", "updated_at"]
    if to_status == obj_status.MET:
        # A manage-capable claim is self-verifying — it counts toward the completion gate at once;
        # a non-manage claim enters met flagged unverified until an officer who is not the claimant
        # signs off (doc 04 §2 lines 68-72, #28).
        if can_manage(user, campaign):
            objective.verified_by = user if getattr(user, "pk", None) else None
            objective.verified_at = timezone.now()
        else:
            objective.verified_by = None
            objective.verified_at = None
        update_fields += ["verified_by", "verified_at"]
    objective.save(update_fields=update_fields)

    record_activity(
        campaign, user, "objective.status", target_kind="objective", target_id=objective.pk,
        before={"status": old}, after={"status": to_status}, reason=(reason or note or "")[:300],
    )
    if to_status in (obj_status.MET, obj_status.DROPPED):
        resolve_dependencies_for(DependencyKind.OBJECTIVE, objective.pk)
    _recompute_locked(locked_campaign)
    _mirror(campaign, locked_campaign)
    transaction.on_commit(lambda: notify.health_changed(campaign))
    return objective


@transaction.atomic
def verify_objective(objective, user):
    """Officer sign-off on a met claim (doc 07 §1.1). Requires officer rank and verifier ≠
    claimant; only a currently-``met`` objective can be verified. Re-fetches the objective under
    the campaign row lock so a status change that landed after the form loaded (e.g. the objective
    is no longer ``met``) loses cleanly instead of stamping a stale row (#24)."""
    campaign = objective.campaign
    locked_campaign = Campaign.objects.select_for_update().get(pk=campaign.pk)
    objective = locked_campaign.objectives.select_for_update().get(pk=objective.pk)
    if objective.status != Objective.ObjectiveStatus.MET:
        raise ValidationError("Only a met objective can be verified.")
    if not can_verify(user, objective):
        raise ValidationError(
            "You cannot verify this objective — officer rank is required and the verifier must "
            "differ from the pilot who claimed it."
        )
    now = timezone.now()
    objective.verified_by = user
    objective.verified_at = now
    objective.save(update_fields=["verified_by", "verified_at", "updated_at"])
    record_activity(
        campaign, user, "objective.verified", target_kind="objective",
        target_id=objective.pk, after={"verified": True},
    )
    _recompute_locked(locked_campaign)
    _mirror(campaign, locked_campaign)
    transaction.on_commit(lambda: notify.health_changed(campaign))
    return objective


def _met_claimant(objective):
    """The pilot who most recently moved the objective to ``met`` (the verification counterpart)."""
    rows = (
        CampaignActivity.objects
        .filter(campaign_id=objective.campaign_id, target_kind="objective",
                target_id=objective.pk, verb="objective.status")
        .order_by("-created_at", "-id")
    )
    for row in rows:
        if (row.after or {}).get("status") == Objective.ObjectiveStatus.MET:
            return row.actor
    return None


# --------------------------------------------------------------------------- #
#  Automatic measurement & background sweeps (doc 08, doc 00 §6)
# --------------------------------------------------------------------------- #
_TERMINAL_OBJECTIVE = (
    Objective.ObjectiveStatus.MET, Objective.ObjectiveStatus.MISSED, Objective.ObjectiveStatus.DROPPED,
)

# Tightest-first due-soon buckets (doc 04 §10 D19). ``_due_bucket`` returns the tightest a due
# date currently falls in; as the deadline nears an item moves 7d → 48h → 24h → overdue, each
# bucket firing at most once via its distinct idempotency key.
_DUE_BUCKETS = (
    ("24h", timezone.timedelta(hours=24)),
    ("48h", timezone.timedelta(hours=48)),
    ("7d", timezone.timedelta(days=7)),
)


def _source_min_interval(key: str):
    """The minimum age an auto objective must reach before re-measuring — the per-source rate
    limit on the backing services (doc 08 §4.3): the ``refresh.metrics_minutes`` default, overridden
    per source key by ``refresh.source_minutes``."""
    cfg = config.get("refresh")
    minutes = (cfg.get("source_minutes") or {}).get(key, cfg.get("metrics_minutes", 15))
    return timezone.timedelta(minutes=int(minutes))


def _measure_due(objective, source, now) -> bool:
    """Whether ``objective`` has aged past its source's min interval (never measured ⇒ due)."""
    if objective.measured_at is None:
        return True
    return (now - objective.measured_at) >= _source_min_interval(source.key)


def _manual_stale_days() -> int:
    """Confirmation interval after which a manual objective is nudged / flagged stale (doc 08 §2.2)."""
    return int(config.get("refresh").get("manual_stale_days", 14))


def _auto_objectives(campaign):
    """A campaign's auto, non-paused, non-terminal objectives — the refresh sweep's working set."""
    return campaign.objectives.filter(measurement_paused=False).exclude(
        metric_source=""
    ).exclude(status__in=_TERMINAL_OBJECTIVE)


def _apply_measurement(campaign, objective, source, measurement) -> bool:
    """Persist one successful auto measurement onto ``objective`` (assumes the campaign row is
    locked). Writes ``current_value``/``measured_at``/``measurement_source=auto`` + an
    ``ObjectiveSample``, records an automation activity row **only when the value changed** (doc 08
    §2.1 step 4), and — when the objective *newly* reaches its target — a ``objective.target_reached``
    prompt row. It never changes status: meeting the number is a prompt for a human to close, not an
    auto-close (doc 08 §3 trigger 1). Returns whether the stored value changed.
    """
    old_value = objective.current_value
    old_progress = objective.progress_pct
    changed = old_value is None or Decimal(old_value) != measurement.value

    objective.current_value = measurement.value
    objective.measured_at = measurement.as_of
    objective.measurement_source = MeasurementSource.AUTO
    objective.progress_pct = objective_progress(objective)
    objective.save(update_fields=[
        "current_value", "measured_at", "measurement_source", "progress_pct", "updated_at",
    ])
    ObjectiveSample.objects.create(
        objective=objective, value=measurement.value, measured_at=measurement.as_of,
        source=MeasurementSource.AUTO, note=str(measurement.detail.get("note", ""))[:200],
    )
    if changed:
        record_activity(
            campaign, None, "objective.progress", target_kind="objective", target_id=objective.pk,
            before={"current_value": _activity_value(objective, old_value)},
            after={"current_value": _activity_value(objective, measurement.value)}, source="automation",
        )
    if old_progress < 100 and objective.progress_pct >= 100:
        record_activity(
            campaign, None, "objective.target_reached", target_kind="objective",
            target_id=objective.pk,
            after={"current_value": _activity_value(objective, measurement.value)},
            source="automation",
        )
    return changed


def measure_objective(objective) -> bool:
    """Measure + persist one auto objective on demand (the single-objective 'refresh now' path).

    Skips a manual / paused / unknown-source objective and one still inside its source's min
    interval — the same gating the sweep applies (doc 04 §3.2, doc 08 §2.1) — then writes under the
    campaign row lock and recomputes. Returns whether the stored value changed.
    """
    if not objective.metric_source or objective.measurement_paused:
        return False
    source = metrics.get_source(objective.metric_source)
    if source is None:
        logger.warning("campaigns.measure unknown metric_source=%s objective=%s",
                       objective.metric_source, objective.pk)
        return False
    if not _measure_due(objective, source, timezone.now()):
        return False
    measurement = metrics.measure_safely(source, metrics.build_call_params(objective))
    if measurement is None:
        return False

    campaign = objective.campaign
    with transaction.atomic():
        locked = Campaign.objects.select_for_update().get(pk=campaign.pk)
        changed = _apply_measurement(locked, objective, source, measurement)
        _recompute_locked(locked)
        _mirror(campaign, locked)
    transaction.on_commit(lambda: notify.health_changed(campaign))
    return changed


def refresh_campaign(campaign) -> dict:
    """Measure every due auto objective of one campaign, then recompute once (doc 08 §2.1).

    Sources are measured *before* the write transaction so their backing reads never extend the
    campaign row lock; the value writes + the single progress/health recompute then happen under
    ``select_for_update`` on the campaign row (the guard against a simultaneous officer edit
    clobbering the recompute). Returns the per-campaign summary the sweep sums.
    """
    now = timezone.now()
    counts = {"refreshed": 0, "skipped_fresh": 0, "errors": 0, "health_changed": 0}
    pending = []
    for obj in _auto_objectives(campaign):
        source = metrics.get_source(obj.metric_source)
        if source is None:
            logger.warning("campaigns.refresh unknown metric_source=%s objective=%s",
                           obj.metric_source, obj.pk)
            continue
        if not _measure_due(obj, source, now):
            counts["skipped_fresh"] += 1
            continue
        measurement = metrics.measure_safely(source, metrics.build_call_params(obj))
        if measurement is None:
            counts["errors"] += 1
            continue
        pending.append((obj, source, measurement))

    if not pending:
        return counts

    old_health = campaign.health
    with transaction.atomic():
        locked = Campaign.objects.select_for_update().get(pk=campaign.pk)
        for obj, source, measurement in pending:
            # Re-read each objective under the campaign lock and skip it if a pause, a manual
            # correction, or a terminal transition landed in the measurement window — a paused or
            # moved objective must never be clobbered with an AUTO sample (doc 04 §3.2, #25).
            fresh = locked.objectives.select_for_update().filter(pk=obj.pk).first()
            if fresh is None or fresh.measurement_paused or fresh.status in _TERMINAL_OBJECTIVE:
                counts["skipped_fresh"] += 1
                continue
            if fresh.measured_at != obj.measured_at:  # a newer sample landed meanwhile
                counts["skipped_fresh"] += 1
                continue
            _apply_measurement(locked, fresh, source, measurement)
            counts["refreshed"] += 1
        _recompute_locked(locked)
        _mirror(campaign, locked)
    if campaign.health != old_health:
        counts["health_changed"] = 1
    transaction.on_commit(lambda: notify.health_changed(campaign))
    return counts


def run_metric_refresh() -> dict:
    """``campaigns.refresh_metrics`` beat body: sweep every ACTIVE (not paused) campaign.

    Idempotent — a second immediate run finds every objective inside its min interval and is a
    no-op (``skipped_fresh``); a mid-sweep crash re-delivers and picks up only the still-due
    objectives, because each campaign's writes are one transaction (doc 08 §2.1)."""
    import time

    started = time.monotonic()
    summary = {"refreshed": 0, "skipped_fresh": 0, "errors": 0, "health_changes": 0, "campaigns": 0}
    if not config.get("refresh").get("enabled", True):
        summary["status"] = "disabled"  # documented kill-switch (doc 08 §2.1)
        logger.info("campaigns.refresh_metrics %s", summary)
        return summary
    for campaign in Campaign.objects.filter(status=Campaign.Status.ACTIVE):
        try:
            result = refresh_campaign(campaign)
        except Exception:  # noqa: BLE001 — one campaign's failure (e.g. a lock timeout) never aborts the sweep
            summary["errors"] += 1
            summary["campaigns"] += 1
            logger.exception("campaigns.refresh_metrics campaign=%s failed", campaign.pk)
            continue
        summary["refreshed"] += result["refreshed"]
        summary["skipped_fresh"] += result["skipped_fresh"]
        summary["errors"] += result["errors"]
        summary["health_changes"] += result["health_changed"]
        summary["campaigns"] += 1
    summary["duration_ms"] = int((time.monotonic() - started) * 1000)
    logger.info("campaigns.refresh_metrics %s", summary)
    return summary


def _due_bucket(due_at, now) -> str | None:
    """The tightest due-soon bucket a due date currently sits in, or ``None`` when too far out."""
    if due_at is None:
        return None
    if due_at < now:
        return "overdue"
    delta = due_at - now
    for name, window in _DUE_BUCKETS:
        if delta <= window:
            return name
    return None


def run_deadline_sweep() -> dict:
    """``campaigns.sweep_deadlines`` beat body: due-soon / overdue reminders for objectives and
    milestones, plus stale-manual nudges, across ACTIVE campaigns (doc 08 §2.2).

    The sweep holds no state — pingboard's alert table *is* the 'already sent' ledger. It pre-loads
    the campaign idempotency keys already emitted so a re-run counts them as ``suppressed`` and
    emits nothing new. Each emission is fail-soft; the health recheck catches the time-driven rules
    (past target date, inactivity) a refresh may not have re-evaluated."""
    now = timezone.now()
    summary = {"due_soon": 0, "overdue": 0, "blocked": 0, "manual_stale": 0,
               "suppressed": 0, "errors": 0}

    from apps.pingboard.models import Alert

    stale_cutoff = now - timezone.timedelta(days=_manual_stale_days())
    year, week, _ = now.isocalendar()
    iso_week = f"{year}-W{week:02d}"
    # Preload only the idempotency keys that could still suppress an emission this run, not every
    # historical key (which is bounded only by pingboard's 365-day retention): due-reminder keys
    # from the last few days (the widest soon bucket is 7d) plus this ISO week's manual-nudge keys
    # (those rotate weekly, so older weeks are irrelevant). pingboard's own persistent idempotency
    # is the real dedup backstop, so a key that ages out of this window is still deduped at emit
    # time — the scoped preload only trims memory and the redundant notify call (#39).
    due_since = now - timezone.timedelta(days=8)
    sent = set(
        Alert.objects.filter(source_service="campaigns")
        .filter(
            (Q(idempotency_key__startswith="campaigns:due:") & Q(created_at__gte=due_since))
            | (Q(idempotency_key__startswith="campaigns:manual:")
               & Q(idempotency_key__endswith=f":{iso_week}"))
        )
        .values_list("idempotency_key", flat=True)
    )
    # Documented kill-switch for the due-soon/overdue reminders (doc 08 §2.2, doc 09 §7); the
    # manual-staleness nudge and the time-driven health recheck are independent of it.
    reminders_on = bool(config.get("notifications").get("deadline_reminders", True))

    for campaign in Campaign.objects.filter(status=Campaign.Status.ACTIVE):
        try:
            if reminders_on:
                _sweep_campaign_deadlines(campaign, now, sent, summary)
            _sweep_campaign_manual(campaign, stale_cutoff, iso_week, sent, summary)
            recompute(campaign)  # cheap time-driven health recheck (doc 08 §2.2)
        except Exception:  # noqa: BLE001 — one campaign's failure never stops the sweep
            summary["errors"] += 1
            logger.exception("campaigns.sweep_deadlines campaign=%s failed", campaign.pk)
    logger.info("campaigns.sweep_deadlines %s", summary)
    return summary


def _sweep_campaign_deadlines(campaign, now, sent, summary) -> None:
    """Emit due-soon / overdue reminders for one campaign's objectives and milestones."""
    obj_status = Objective.ObjectiveStatus
    objectives = campaign.objectives.filter(due_at__isnull=False).exclude(
        status__in=_TERMINAL_OBJECTIVE
    )
    for obj in objectives:
        if obj.status == obj_status.BLOCKED:
            # Blocked re-notification is signature-driven, never on a timer (doc 08 §2.2); count only.
            summary["blocked"] += 1
        bucket = _due_bucket(obj.due_at, now)
        if bucket is not None:
            _emit_deadline(campaign, "objective", obj, bucket, obj.owner_id, sent, summary)

    ms_status = Milestone.MilestoneStatus
    milestones = campaign.milestones.filter(due_at__isnull=False).exclude(
        status__in=[ms_status.DONE, ms_status.MISSED]
    )
    for ms in milestones:
        bucket = _due_bucket(ms.due_at, now)
        if bucket is not None:
            _emit_deadline(campaign, "milestone", ms, bucket, ms.owner_id, sent, summary)


def _emit_deadline(campaign, kind, item, bucket, owner_id, sent, summary) -> None:
    """One deadline emission through ``notify.deadline_soon``, idempotency-key deduped."""
    key = f"campaigns:due:{kind}:{item.pk}:{bucket}"
    if key in sent:
        summary["suppressed"] += 1
        return
    notify.deadline_soon(campaign, kind, item, bucket, owner_id=owner_id, title=item.title)
    sent.add(key)
    summary["overdue" if bucket == "overdue" else "due_soon"] += 1


def _sweep_campaign_manual(campaign, stale_cutoff, iso_week, sent, summary) -> None:
    """Nudge the owners of manual objectives whose last reading has gone stale (once per ISO week)."""
    stale = campaign.objectives.filter(
        metric_source="", measurement_paused=False,
        measured_at__isnull=False, measured_at__lt=stale_cutoff,
    ).exclude(status__in=_TERMINAL_OBJECTIVE)
    for obj in stale:
        key = f"campaigns:manual:{obj.pk}:{iso_week}"
        if key in sent:
            summary["suppressed"] += 1
            continue
        notify.manual_update_needed(campaign, obj, iso_week, owner_id=obj.owner_id)
        sent.add(key)
        summary["manual_stale"] += 1


def run_housekeeping() -> dict:
    """``campaigns.housekeeping`` beat body: retention pruning (doc 08 §2.3).

    Pure delete-older-than operations, batched so a mid-run crash leaves a smaller still-valid
    dataset for the next night. Each step is isolated so one failure never blocks the other."""
    summary = {"samples_pruned": 0, "archived_samples_pruned": 0, "activity_pruned": 0, "errors": 0}
    retention = config.get("retention")
    now = timezone.now()
    try:
        cutoff = now - timezone.timedelta(days=int(retention["samples_days"]))
        summary["samples_pruned"] = _prune_samples(cutoff)
    except Exception:  # noqa: BLE001 — a prune failure self-heals on the next nightly run
        summary["errors"] += 1
        logger.exception("campaigns.housekeeping sample prune failed")
    try:
        # Archived campaigns keep their samples on a tighter clock (doc 06 §8, #41).
        cutoff = now - timezone.timedelta(days=int(retention["archived_sample_retention_days"]))
        summary["archived_samples_pruned"] = _prune_archived_samples(cutoff)
    except Exception:  # noqa: BLE001
        summary["errors"] += 1
        logger.exception("campaigns.housekeeping archived sample prune failed")
    try:
        cutoff = now - timezone.timedelta(days=int(retention["activity_days"]))
        summary["activity_pruned"] = _prune_activity(cutoff)
    except Exception:  # noqa: BLE001
        summary["errors"] += 1
        logger.exception("campaigns.housekeeping activity prune failed")
    logger.info("campaigns.housekeeping %s", summary)
    return summary


def _prune_samples(cutoff) -> int:
    """Delete ``ObjectiveSample`` rows older than ``cutoff``, always keeping the newest per objective
    so a long-idle objective retains its last reading for the sparkline/freshness display."""
    from django.db.models import F, OuterRef, Subquery

    newest = (
        ObjectiveSample.objects.filter(objective_id=OuterRef("objective_id"))
        .order_by("-measured_at", "-id")
    )
    stale = (
        ObjectiveSample.objects.filter(measured_at__lt=cutoff)
        .annotate(newest_id=Subquery(newest.values("pk")[:1]))
        .exclude(pk=F("newest_id"))
    )
    return _batch_delete(ObjectiveSample, stale)


def _prune_archived_samples(cutoff) -> int:
    """Delete ``ObjectiveSample`` rows of ARCHIVED campaigns older than ``cutoff`` — the tighter
    archived retention of doc 06 §8 — always keeping the newest per objective so the report's
    sparkline still has its last reading (#41)."""
    from django.db.models import F, OuterRef, Subquery

    newest = (
        ObjectiveSample.objects.filter(objective_id=OuterRef("objective_id"))
        .order_by("-measured_at", "-id")
    )
    stale = (
        ObjectiveSample.objects.filter(
            measured_at__lt=cutoff,
            objective__campaign__status=Campaign.Status.ARCHIVED,
        )
        .annotate(newest_id=Subquery(newest.values("pk")[:1]))
        .exclude(pk=F("newest_id"))
    )
    return _batch_delete(ObjectiveSample, stale)


def _prune_activity(cutoff) -> int:
    """Delete ``CampaignActivity`` rows older than ``cutoff`` for ARCHIVED campaigns only — sensitive
    verbs also live in ``core.audit.AuditLog`` (its 730-day floor is never touched here, doc 08 §2.3)."""
    stale = CampaignActivity.objects.filter(
        created_at__lt=cutoff, campaign__status=Campaign.Status.ARCHIVED,
    )
    return _batch_delete(CampaignActivity, stale)


def _batch_delete(model, queryset, batch=1000) -> int:
    """Delete ``queryset`` in id-chunks so a mid-run crash leaves a smaller, still-valid dataset."""
    total = 0
    while True:
        ids = list(queryset.values_list("pk", flat=True)[:batch])
        if not ids:
            break
        model.objects.filter(pk__in=ids).delete()
        total += len(ids)
        if len(ids) < batch:
            break
    return total


# --------------------------------------------------------------------------- #
#  Linked tasks (doc 04 §10, doc 10 §6.3)
# --------------------------------------------------------------------------- #
def create_objective_task(objective, user, *, title=None, assignee=None, due_at=None,
                          task_type="other"):
    """Create a ``tasks.Task`` soft-linked to an objective through the shared task factory.

    Linked by ``related_type="campaign_objective"`` and a per-objective ``related_id``
    (``"{pk}"`` for the first task, ``"{pk}:{n}"`` for additional ones), so one objective can
    carry several tasks while ``apps.campaigns.signals`` maps every task back to it by the pk
    prefix. Records an activity row; the completion roll-up lives in the signal, never here
    (a human closes the objective — automation may not, doc 04/08).

    A campaign whose visibility is not ``members`` never puts a leaky title/description on the
    corp-wide task board (doc 07 §1.4): the task carries a neutral title/description that names
    neither the campaign nor the objective, and is created **assigned** (default: the acting user)
    so it never sits in the claimable open pool visible to every member/officer.
    """
    from apps.tasks.models import Task
    from apps.tasks.services import create_task

    base = str(objective.pk)
    candidates = [base] + [f"{base}:{n}" for n in range(2, 51)]
    used = set(
        Task.objects.filter(related_type=Objective.RELATED_TYPE, related_id__in=candidates)
        .values_list("related_id", flat=True)
    )
    related_id = next((c for c in candidates if c not in used),
                      f"{base}:{int(timezone.now().timestamp())}")
    if objective.campaign.visibility == Campaign.Visibility.MEMBERS:
        task_title = (title or objective.title)[:200]
        task_description = f"Linked to campaign objective “{objective.title}”."
        task_assignee = assignee
    else:
        task_title = "Campaign follow-up task"
        task_description = "A campaign follow-up task is assigned to you — open Campaign Command for details."
        task_assignee = assignee or (user if getattr(user, "pk", None) else objective.owner)
    task = create_task(
        task_type=task_type,
        title=task_title,
        description=task_description,
        related_type=Objective.RELATED_TYPE, related_id=related_id,
        assignee=task_assignee, due_at=due_at, created_by=user,
    )
    record_activity(
        objective.campaign, user, "objective.task_created", target_kind="objective",
        target_id=objective.pk, after={"task_id": task.pk},
    )
    return task


# --------------------------------------------------------------------------- #
#  Progress & health (doc 00 §4)
# --------------------------------------------------------------------------- #
def objective_progress(objective) -> int:
    """Cached-independent progress percent for one objective (pure math in :mod:`.progress`)."""
    return progress.objective_progress_value(
        objective.baseline_value, objective.target_value, objective.current_value,
        objective.direction,
    )


def campaign_progress(campaign) -> int:
    """Campaign progress percent per ``progress_mode`` (doc 04 §4)."""
    mode = campaign.progress_mode
    if mode == Campaign.ProgressMode.MANUAL:
        return _clamp_pct(campaign.progress_pct or 0)
    if mode == Campaign.ProgressMode.MILESTONES:
        total = campaign.milestones.count()
        if not total:
            return 0
        done = campaign.milestones.filter(status=Milestone.MilestoneStatus.DONE).count()
        return _clamp_pct(round(100 * done / total))
    objectives = [
        obj for obj in campaign.objectives.all()
        if obj.status != Objective.ObjectiveStatus.DROPPED
    ]
    total_weight = sum(obj.weight for obj in objectives)
    if not total_weight:
        return 0
    weighted = sum(objective_progress(obj) * obj.weight for obj in objectives)
    return _clamp_pct(round(weighted / total_weight))


def campaign_health(campaign) -> tuple[str, list[dict]]:
    """Deterministic ``(state, reasons)`` for a campaign (pure rules in :mod:`.health`)."""
    thresholds = config.get("health")
    return health.evaluate(_health_facts(campaign, thresholds), thresholds)


def recompute(campaign):
    """Persist ``progress_pct`` + ``health`` (and each objective's cached progress) under the
    campaign row lock so a concurrent refresh cannot clobber the write (doc 05 §5)."""
    with transaction.atomic():
        locked = Campaign.objects.select_for_update().get(pk=campaign.pk)
        _recompute_locked(locked)
        _mirror(campaign, locked)
    # A health-level/reason change pings leadership once per distinct signature (doc 09 §4);
    # scheduled after commit so a rolled-back recompute never leaks a notification.
    transaction.on_commit(lambda: notify.health_changed(campaign))
    return campaign


@transaction.atomic
def set_manual_progress(campaign, user, pct, note):
    """Set a ``manual``-mode campaign's progress by hand (doc 04 §4).

    Manual is the one progress mode with no derived source, so a human sets the number with a
    mandatory provenance note; the author and timestamp are the recorded activity row (doc 04 §4
    mandates all three). Rejected on any other mode — those are recomputed from objectives or
    milestones and would immediately overwrite a hand-set figure. ``campaign_progress`` reads back
    the stored ``progress_pct`` for manual mode, so the trailing ``recompute`` is a fixed point that
    keeps the value while refreshing health. Manage-gated in the view."""
    if campaign.progress_mode != Campaign.ProgressMode.MANUAL:
        raise ValidationError("Manual progress can only be set on a manual-progress campaign.")
    if not (note or "").strip():
        raise ValidationError("A note is required when setting progress manually.")
    try:
        value = _clamp_pct(int(pct))
    except (TypeError, ValueError) as exc:
        raise ValidationError("Progress must be a whole number between 0 and 100.") from exc

    before = campaign.progress_pct
    campaign.progress_note = note.strip()
    campaign.progress_pct = value
    campaign.save(update_fields=["progress_pct", "progress_note", "updated_at"])
    record_activity(
        campaign, user, "progress.manual", target_kind="campaign", target_id=campaign.pk,
        before={"progress_pct": before}, after={"progress_pct": value}, reason=note.strip()[:300],
    )
    recompute(campaign)
    return campaign


def _recompute_locked(campaign) -> None:
    """Recompute objective + campaign progress and health assuming the row is already locked."""
    for obj in campaign.objectives.all():
        value = objective_progress(obj)
        if obj.progress_pct != value:
            obj.progress_pct = value
            obj.save(update_fields=["progress_pct", "updated_at"])
    campaign.progress_pct = campaign_progress(campaign)
    campaign.health, campaign.health_reasons = campaign_health(campaign)
    campaign.save(update_fields=["progress_pct", "health", "health_reasons", "updated_at"])


def _health_facts(campaign, thresholds) -> dict:
    """Assemble the boolean/count facts the pure health evaluator scores (doc 00 §4)."""
    now = timezone.now()
    status = Campaign.Status
    obj_status = Objective.ObjectiveStatus
    if campaign.status not in (status.ACTIVE, status.PAUSED):
        return {"not_active": True}

    objectives = list(campaign.objectives.all())
    non_dropped = [o for o in objectives if o.status != obj_status.DROPPED]
    mode = campaign.progress_mode
    if mode == Campaign.ProgressMode.MANUAL:
        measurable = True
    elif mode == Campaign.ProgressMode.MILESTONES:
        measurable = campaign.milestones.exists()
    else:
        measurable = bool(non_dropped) and any(o.current_value is not None for o in non_dropped)
    if not measurable:
        return {"no_measurable": True}

    terminal = {obj_status.MET, obj_status.MISSED, obj_status.DROPPED}
    facts: dict = {
        "mandatory_blocked": any(
            o.is_mandatory and o.status == obj_status.BLOCKED for o in objectives
        ),
        "blocked_nonmandatory": sum(
            1 for o in objectives if not o.is_mandatory and o.status == obj_status.BLOCKED
        ),
        "overdue_objectives": sum(
            1 for o in objectives if o.due_at and o.due_at < now and o.status not in terminal
        ),
        "unowned_objectives": sum(
            1 for o in objectives
            if o.owner_id is None and o.status in (obj_status.PENDING, obj_status.ACTIVE)
        ),
        "escalated_issues": campaign.issues.filter(status=Issue.IssueStatus.ESCALATED).count(),
        "risk_sev9_overdue": campaign.risks.filter(
            status__in=[Risk.RiskStatus.OPEN, Risk.RiskStatus.MITIGATING],
            severity=9, due_at__lt=now,
        ).exists(),
        "stale_metrics": _stale_auto_count(non_dropped, thresholds, now),
    }

    facts["past_deadline"] = False
    facts["deadline_shortfall"] = None
    if campaign.target_end_at:
        if campaign.target_end_at < now and campaign.progress_pct < 100:
            facts["past_deadline"] = True
        elif campaign.start_at and campaign.start_at < now < campaign.target_end_at:
            span = (campaign.target_end_at - campaign.start_at).total_seconds()
            if span > 0:
                expected = (now - campaign.start_at).total_seconds() / span * 100
                facts["deadline_shortfall"] = int(expected - campaign.progress_pct)

    facts["budget_ratio"] = None
    if campaign.budget_isk and campaign.budget_isk > 0:
        facts["budget_ratio"] = float(campaign.spent_isk) / float(campaign.budget_isk)

    facts["dep_blocked_mandatory"], facts["dep_blocker_any"] = _dependency_blockers(campaign)

    last_activity = campaign.activity.order_by("-created_at").first()
    facts["inactive"] = bool(
        last_activity
        and (now - last_activity.created_at).days >= thresholds["inactivity_days"]
    )
    return facts


def _stale_auto_count(objectives, thresholds, now) -> int:
    """How many measured auto objectives are stale past the ``watch`` threshold (doc 00 §4).

    Stale = an auto (non-paused) objective whose ``measured_at`` is older than its source's
    ``core.freshness`` threshold × the configured ``stale_multiplier`` (default 2 — a wider band
    than the UI freshness chip so a single late sync does not flip health). A never-measured auto
    objective is *unmeasured*, not stale, and is excluded here (it contributes 0 to progress and,
    if it is the whole picture, drives ``unknown`` health instead).
    """
    multiplier = thresholds.get("stale_multiplier", 2)
    stale = 0
    for obj in objectives:
        if not obj.metric_source or obj.measurement_paused or obj.measured_at is None:
            continue
        source = metrics.get_source(obj.metric_source)
        data_class = source.data_class if source else "default"
        threshold = freshness.THRESHOLDS.get(data_class, freshness.THRESHOLDS["default"])
        if (now - obj.measured_at) > threshold * multiplier:
            stale += 1
    return stale


def _dependency_blockers(campaign) -> tuple[bool, bool]:
    """``(blocks_a_mandatory_objective, blocks_anything)`` for unresolved edges whose target sits
    in a bad terminal state (doc 04 §8)."""
    mandatory = False
    any_blocker = False
    for dep in campaign.dependencies.filter(is_resolved=False):
        if not _dependency_target_blocking(dep):
            continue
        any_blocker = True
        if dep.from_kind == DependencyKind.OBJECTIVE:
            src = campaign.objectives.filter(pk=dep.from_id).first()
            if src and src.is_mandatory:
                mandatory = True
    return mandatory, any_blocker


def _dependency_target_blocking(dep) -> bool:
    """Whether an edge's target is in a state that makes the edge a live blocker."""
    kind = DependencyKind
    if dep.to_kind == kind.OBJECTIVE:
        target = Objective.objects.filter(pk=dep.to_id).first()
        return bool(target and target.status in (
            Objective.ObjectiveStatus.BLOCKED, Objective.ObjectiveStatus.MISSED,
        ))
    if dep.to_kind == kind.MILESTONE:
        target = Milestone.objects.filter(pk=dep.to_id).first()
        return bool(target and target.status == Milestone.MilestoneStatus.MISSED)
    if dep.to_kind == kind.CAMPAIGN:
        target = Campaign.objects.filter(pk=dep.to_id).first()
        return bool(target and target.status in (
            Campaign.Status.FAILED, Campaign.Status.CANCELLED,
        ))
    return False


def _clamp_pct(value) -> int:
    return max(0, min(100, int(value)))


def _num(value):
    """JSON-safe representation of a Decimal (kept as a string so precision survives)."""
    return None if value is None else str(value)


_REDACTED = "<redacted>"


def _activity_value(objective, value):
    """A JSON-safe measurement for an activity ``before``/``after`` — redacted for a sensitive
    objective so the pull-based feed can never leak a restricted figure; the real value lives only
    in ``core.audit`` (doc 07 T10, #30)."""
    return _REDACTED if objective.is_sensitive else _num(value)


# --------------------------------------------------------------------------- #
#  Dependencies (doc 04 §8)
# --------------------------------------------------------------------------- #
@transaction.atomic
def add_dependency(campaign, from_kind, from_id, to_kind, to_id=0, note="", user=None):
    """Create a ``from`` blocked-by ``to`` edge, rejecting self-edges, cycles and over-deep
    chains (doc 04 §8). ``external`` is valid only as ``to_kind`` (``to_id=0`` + a note). A
    duplicate edge is an idempotent no-op returning the existing row (doc 07 T14)."""
    kind = DependencyKind
    if from_kind == kind.EXTERNAL:
        raise ValidationError("A dependency's source cannot be external.")
    from_id = int(from_id)
    # The source must belong to this campaign — never a free-typed bare pk into another campaign's
    # entities (doc 07 bare-PK + no-oracle rules, #4).
    _require_dependency_source(campaign, from_kind, from_id)
    if to_kind == kind.EXTERNAL:
        to_id = 0
        if not (note or "").strip():
            raise ValidationError("An external dependency needs a note describing it.")
    else:
        to_id = int(to_id)
        if from_kind == to_kind and from_id == to_id:
            raise ValidationError("A dependency cannot point at itself.")
        # A non-external target must exist and be reachable, so auto-resolution never becomes an
        # existence/lifecycle oracle on a campaign the actor cannot see (#4).
        _require_dependency_target(campaign, to_kind, to_id, user)

    _reject_cycle(campaign, (from_kind, from_id), (to_kind, to_id))

    try:
        with transaction.atomic():
            dep = CampaignDependency.objects.create(
                campaign=campaign, from_kind=from_kind, from_id=from_id,
                to_kind=to_kind, to_id=to_id, note=(note or "")[:200],
                created_by=user if getattr(user, "pk", None) else None,
            )
    except IntegrityError:
        return CampaignDependency.objects.get(
            campaign=campaign, from_kind=from_kind, from_id=from_id,
            to_kind=to_kind, to_id=to_id,
        )
    record_activity(
        campaign, user, "dependency.added", target_kind="dependency", target_id=dep.pk,
        after={"from": f"{from_kind}:{from_id}", "to": f"{to_kind}:{to_id}"}, reason=note,
    )
    return dep


def _require_dependency_source(campaign, from_kind, from_id) -> None:
    """A dependency edge's ``from`` endpoint must be one of this campaign's own entities (#4)."""
    kind = DependencyKind
    if from_kind == kind.OBJECTIVE:
        ok = campaign.objectives.filter(pk=from_id).exists()
    elif from_kind == kind.MILESTONE:
        ok = campaign.milestones.filter(pk=from_id).exists()
    elif from_kind == kind.WORKSTREAM:
        ok = campaign.workstreams.filter(pk=from_id).exists()
    elif from_kind == kind.CAMPAIGN:
        ok = from_id == campaign.pk
    else:
        ok = False
    if not ok:
        raise ValidationError("The dependency's source must be part of this campaign.")


def _require_dependency_target(campaign, to_kind, to_id, user) -> None:
    """A non-external ``to`` endpoint must exist and be reachable: an in-campaign objective/
    milestone/workstream, or a campaign the actor may view (never a bare-pk oracle, #4)."""
    kind = DependencyKind
    if to_kind == kind.OBJECTIVE:
        ok = campaign.objectives.filter(pk=to_id).exists()
    elif to_kind == kind.MILESTONE:
        ok = campaign.milestones.filter(pk=to_id).exists()
    elif to_kind == kind.WORKSTREAM:
        ok = campaign.workstreams.filter(pk=to_id).exists()
    elif to_kind == kind.CAMPAIGN:
        target = Campaign.objects.filter(pk=to_id).first()
        ok = bool(target and (user is None or can_view(user, target)))
    else:
        ok = False
    if not ok:
        raise ValidationError("The blocking target must be an item of this campaign or a campaign you can view.")


def _reject_cycle(campaign, from_node, to_node, cap=_DEPENDENCY_DEPTH_CAP) -> None:
    """Walk existing edges from ``to_node``; reject if the walk reaches ``from_node`` (a cycle)
    or exceeds the depth cap (the DoS guard, doc 07 T20). Campaign and external targets are
    exempt — they cannot cycle within one campaign's graph."""
    kind = DependencyKind
    if to_node[0] in (kind.CAMPAIGN, kind.EXTERNAL):
        return
    edges = defaultdict(list)
    for dep in campaign.dependencies.exclude(to_kind=kind.EXTERNAL):
        edges[(dep.from_kind, dep.from_id)].append((dep.to_kind, dep.to_id))

    stack = [(to_node, 0)]
    seen = set()
    while stack:
        node, depth = stack.pop()
        if node == from_node:
            raise ValidationError("This dependency would create a cycle.")
        if depth >= cap:
            raise ValidationError("This dependency chain is too deep to add safely.")
        if node in seen:
            continue
        seen.add(node)
        for nxt in edges.get(node, []):
            stack.append((nxt, depth + 1))


def resolve_dependencies_for(kind: str, target_id) -> int:
    """Auto-resolve every unresolved edge pointing at ``(kind, target_id)`` — called when the
    target reaches a terminal-done state (doc 04 §8). Returns the number resolved."""
    edges = list(CampaignDependency.objects.filter(
        to_kind=kind, to_id=int(target_id), is_resolved=False,
    ))
    for edge in edges:
        edge.is_resolved = True
        edge.save(update_fields=["is_resolved", "updated_at"])
        record_activity(
            edge.campaign, None, "dependency.resolved", target_kind="dependency",
            target_id=edge.pk, after={"is_resolved": True}, source="automation",
        )
        # DM the owners of the now-unblocked ``from`` items + the commander (doc 09 §4).
        owner_ids = _dependency_from_owner_ids(edge)
        transaction.on_commit(
            lambda e=edge, ids=owner_ids: notify.dependency_completed(e, ids)
        )
    return len(edges)


def _dependency_from_owner_ids(edge) -> list:
    """The owner/lead/commander id(s) of a dependency edge's ``from`` endpoint — who to notify
    when the edge auto-resolves (doc 09 §4). Empty for external/missing endpoints."""
    kind = DependencyKind
    if edge.from_kind == kind.OBJECTIVE:
        row = Objective.objects.filter(pk=edge.from_id).first()
        return [row.owner_id] if row and row.owner_id else []
    if edge.from_kind == kind.MILESTONE:
        row = Milestone.objects.filter(pk=edge.from_id).first()
        return [row.owner_id] if row and row.owner_id else []
    if edge.from_kind == kind.WORKSTREAM:
        from .models import Workstream

        row = Workstream.objects.filter(pk=edge.from_id).first()
        return [row.lead_id] if row and row.lead_id else []
    if edge.from_kind == kind.CAMPAIGN:
        row = Campaign.objects.filter(pk=edge.from_id).first()
        return [row.commander_id] if row and row.commander_id else []
    return []


# --------------------------------------------------------------------------- #
#  Risks & issues (doc 04 §7)
# --------------------------------------------------------------------------- #
def compute_severity(probability, impact) -> int:
    """Risk severity = probability × impact in 1..9 (doc 06 §4.13)."""
    return _RISK_WEIGHT.get(probability, 2) * _RISK_WEIGHT.get(impact, 2)


@transaction.atomic
def save_risk(risk, user=None):
    """Persist a risk with its severity recomputed from probability × impact (never taken from
    input — the mass-assignment guard of doc 06 §7). Audited as ``campaigns.risk_changed``."""
    risk.severity = compute_severity(risk.probability, risk.impact)
    risk.save()
    record_activity(
        risk.campaign, user, "risk.saved", target_kind="risk", target_id=risk.pk,
        after={"severity": risk.severity, "status": risk.status},
    )
    audit_log(
        user, "campaigns.risk_changed", target_type="campaign", target_id=str(risk.campaign_id),
        metadata={"risk_id": risk.pk, "severity": risk.severity, "status": risk.status},
    )
    # A severity-9 overdue risk is a health input (``risk_sev9_overdue``), so refresh health now
    # instead of leaving the campaign stale until the hourly sweep (#36).
    recompute(risk.campaign)
    return risk


@transaction.atomic
def raise_issue(campaign, user, description, objective=None, effect="", owner=None,
                target_resolution_at=None):
    """Open an issue; if it is linked to an objective, block that objective (doc 04 §7)."""
    if not (description or "").strip():
        raise ValidationError("An issue needs a description.")
    # A terminal objective (met/missed/dropped) can't be blocked — blocking one would resurrect a
    # reason-audited resolution when the issue later clears (doc 04 §7, #13).
    if objective is not None and objective.status in _TERMINAL_OBJECTIVE:
        raise ValidationError("You can't raise a blocking issue against an already-resolved objective.")
    issue = Issue.objects.create(
        campaign=campaign, objective=objective, description=description, effect=effect or "",
        owner=owner if getattr(owner, "pk", None) else None,
        raised_by=user if getattr(user, "pk", None) else None,
        target_resolution_at=target_resolution_at, status=Issue.IssueStatus.OPEN,
    )
    record_activity(
        campaign, user, "issue.raised", target_kind="issue", target_id=issue.pk,
        after={"status": Issue.IssueStatus.OPEN},
    )
    if objective is not None:
        _block_objective(objective, user, reason=(effect or description)[:300])
        # Owner + commander DM, keyed by the blocking-set signature so a new cause re-notifies
        # while the same cause never re-pings (doc 09 §4).
        blockers = list(
            Issue.objects.filter(
                objective=objective,
                status__in=[Issue.IssueStatus.OPEN, Issue.IssueStatus.ESCALATED],
            ).values_list("pk", flat=True)
        )
        transaction.on_commit(
            lambda o=objective, b=blockers: notify.objective_blocked(o, blockers=b)
        )
    recompute(campaign)
    return issue


@transaction.atomic
def escalate_issue(issue, user, reason):
    """Escalate an open issue (doc 04 §7): mandatory reason, records activity + audit, re-runs
    health (the escalated-issue rule → ``at_risk``), and DMs leadership. Idempotent — escalating
    an already-escalated issue is a no-op; a resolved issue cannot be escalated."""
    if not (reason or "").strip():
        raise ValidationError("A reason is required to escalate an issue.")
    if issue.status == Issue.IssueStatus.RESOLVED:
        raise ValidationError("A resolved issue cannot be escalated.")
    if issue.status == Issue.IssueStatus.ESCALATED:
        return issue
    issue.status = Issue.IssueStatus.ESCALATED
    issue.escalated_at = timezone.now()
    issue.save(update_fields=["status", "escalated_at", "updated_at"])
    record_activity(
        issue.campaign, user, "issue.escalated", target_kind="issue", target_id=issue.pk,
        before={"status": Issue.IssueStatus.OPEN}, after={"status": Issue.IssueStatus.ESCALATED},
        reason=(reason or "")[:300],
    )
    audit_log(
        user, "campaigns.issue_escalated", target_type="campaign",
        target_id=str(issue.campaign_id), metadata={"issue_id": issue.pk, "reason": (reason or "")[:300]},
    )
    recompute(issue.campaign)
    transaction.on_commit(lambda: notify.issue_escalated(issue))
    return issue


@transaction.atomic
def resolve_issue(issue, user, resolution_notes):
    """Resolve an issue; if it was the last open/escalated issue blocking its objective, restore
    that objective's pre-block status (the last-issue rule, doc 04 §7)."""
    if not (resolution_notes or "").strip():
        raise ValidationError("Resolution notes are required to resolve an issue.")
    issue.status = Issue.IssueStatus.RESOLVED
    issue.resolution_notes = resolution_notes
    issue.save(update_fields=["status", "resolution_notes", "updated_at"])
    record_activity(
        issue.campaign, user, "issue.resolved", target_kind="issue", target_id=issue.pk,
        after={"status": Issue.IssueStatus.RESOLVED}, reason=(resolution_notes or "")[:300],
    )
    obj = issue.objective
    if obj is not None and obj.status == Objective.ObjectiveStatus.BLOCKED:
        others = Issue.objects.filter(
            objective=obj, status__in=[Issue.IssueStatus.OPEN, Issue.IssueStatus.ESCALATED],
        ).exclude(pk=issue.pk).exists()
        if not others:
            _unblock_objective(obj, user)
    recompute(issue.campaign)
    return issue


def _block_objective(objective, actor, reason="") -> None:
    """Force an objective ``blocked``, retaining its prior status in the activity trail so
    resolution can restore it (doc 04 §7)."""
    obj_status = Objective.ObjectiveStatus
    if objective.status == obj_status.BLOCKED:
        return
    prior = objective.status
    objective.status = obj_status.BLOCKED
    objective.block_reason = (reason or "")[:300]
    objective.save(update_fields=["status", "block_reason", "updated_at"])
    record_activity(
        objective.campaign, actor, "objective.blocked", target_kind="objective",
        target_id=objective.pk, before={"status": prior}, after={"status": obj_status.BLOCKED},
    )


def _unblock_objective(objective, actor) -> None:
    """Restore an objective to the status it held before the block (doc 04 §7)."""
    obj_status = Objective.ObjectiveStatus
    row = (
        CampaignActivity.objects
        .filter(campaign_id=objective.campaign_id, target_kind="objective",
                target_id=objective.pk, verb="objective.blocked")
        .order_by("-created_at", "-id").first()
    )
    prior = (row.before or {}).get("status") if row else None
    if prior not in {obj_status.PENDING, obj_status.ACTIVE, obj_status.MET, obj_status.MISSED,
                     obj_status.DROPPED}:
        prior = obj_status.ACTIVE
    objective.status = prior
    objective.block_reason = ""
    objective.save(update_fields=["status", "block_reason", "updated_at"])
    record_activity(
        objective.campaign, actor, "objective.unblocked", target_kind="objective",
        target_id=objective.pk, before={"status": obj_status.BLOCKED}, after={"status": prior},
    )


# --------------------------------------------------------------------------- #
#  Workstreams & milestones (doc 04 §5, §6)
# --------------------------------------------------------------------------- #
@transaction.atomic
def save_workstream(workstream, user=None):
    """Persist a workstream, deriving a campaign-unique ``key`` slug from its name when unset.

    Keeps the lane's grouping identity stable without asking a user for a slug; the unique
    ``(campaign, key)`` constraint means a collision falls back to a numbered suffix. A lane can
    only be marked ``done`` once its own objectives are terminal (doc 04 line 192, #12), and
    reaching ``done`` auto-resolves dependency edges that were waiting on the lane (doc 04 line 240).
    """
    ws_status = Workstream.WorkstreamStatus
    if workstream.status == ws_status.DONE and workstream.pk:
        live = workstream.objectives.exclude(status__in=_TERMINAL_OBJECTIVE)
        if live.exists():
            raise ValidationError(
                "Finish or drop this lane's objectives before marking the workstream done."
            )
    if not (workstream.key or "").strip():
        base = slugify(workstream.name)[:56] or "lane"
        key = base
        n = 2
        existing = set(
            workstream.campaign.workstreams.exclude(pk=workstream.pk).values_list("key", flat=True)
        )
        while key in existing:
            key = f"{base}-{n}"[:64]
            n += 1
        workstream.key = key
    workstream.save()
    record_activity(
        workstream.campaign, user, "workstream.saved", target_kind="workstream",
        target_id=workstream.pk, after={"status": workstream.status},
    )
    if workstream.status == ws_status.DONE:
        # Idempotent — only unresolved edges are processed, so a later edit never re-fires it.
        resolve_dependencies_for(DependencyKind.WORKSTREAM, workstream.pk)
    return workstream


# Legal milestone transitions (doc 04 §5.4). ``ready_for_review → done`` carries the
# separation-of-duties gate below; ``pending → done`` is deliberately absent so a deliverable is
# always reviewed before it is approved.
_MILESTONE_TRANSITIONS = {
    Milestone.MilestoneStatus.PENDING: {
        Milestone.MilestoneStatus.READY_FOR_REVIEW, Milestone.MilestoneStatus.MISSED,
    },
    Milestone.MilestoneStatus.READY_FOR_REVIEW: {
        Milestone.MilestoneStatus.DONE, Milestone.MilestoneStatus.PENDING,
        Milestone.MilestoneStatus.MISSED,
    },
    # A missed milestone may still be completed late (doc 04 §5.4): missed → ready_for_review →
    # done, recorded honestly with completed_at > due_at.
    Milestone.MilestoneStatus.MISSED: {
        Milestone.MilestoneStatus.PENDING, Milestone.MilestoneStatus.READY_FOR_REVIEW,
    },
    # Correction path only; leaving done clears the approval stamps in set_milestone_status.
    Milestone.MilestoneStatus.DONE: {Milestone.MilestoneStatus.READY_FOR_REVIEW},
}


def save_milestone(milestone, user=None):
    """Persist a milestone definition (title/owner/dates/workstream). Status changes go through
    :func:`set_milestone_status`, never here, so the review gate cannot be bypassed by a save."""
    milestone.save()
    record_activity(
        milestone.campaign, user, "milestone.saved", target_kind="milestone",
        target_id=milestone.pk, after={"status": milestone.status},
    )
    recompute(milestone.campaign)
    # Keep the calendar deadline in step with a definition edit (doc 09 §5) — but only for a
    # campaign the sweep also publishes, so a date-less APPROVED campaign's events don't oscillate
    # (#37).
    if cal.campaign_publishes(milestone.campaign):
        campaign = milestone.campaign
        transaction.on_commit(lambda: cal.publish_campaign(campaign))
    return milestone


@transaction.atomic
def set_milestone_status(milestone, user, to_status):
    """Move a milestone through its status set with the review gate (doc 04 §5).

    ``ready_for_review → done`` is a separation-of-duties gate: the approver must differ from the
    pilot who marked the milestone ready, unless they are a director. Reaching ``done`` stamps
    ``completed_at``/``approved_by`` and auto-resolves dependency edges targeting the milestone;
    milestone-mode campaign progress is recomputed either way.
    """
    ms = Milestone.MilestoneStatus
    if to_status not in {ms.PENDING, ms.READY_FOR_REVIEW, ms.DONE, ms.MISSED}:
        raise ValidationError("Unknown milestone status.")
    if to_status == milestone.status:
        return milestone
    if to_status not in _MILESTONE_TRANSITIONS.get(milestone.status, set()):
        raise ValidationError("That milestone status change is not allowed.")
    if to_status == ms.DONE and milestone.status == ms.READY_FOR_REVIEW:
        marker = _milestone_ready_marker(milestone)
        if (marker is not None and marker.pk == getattr(user, "pk", None)
                and not has_role(user, ROLE_DIRECTOR)):
            raise ValidationError(
                "Someone other than the pilot who marked this milestone ready must approve it."
            )
    if to_status == ms.MISSED and (milestone.due_at is None or milestone.due_at > timezone.now()):
        # A milestone is missed only once its due date has passed (doc 04 §5.4, #27).
        raise ValidationError("A milestone can only be marked missed after its due date has passed.")

    old = milestone.status
    milestone.status = to_status
    update_fields = ["status", "updated_at"]
    if to_status == ms.DONE:
        milestone.completed_at = timezone.now()
        milestone.approved_by = user if getattr(user, "pk", None) else None
        update_fields += ["completed_at", "approved_by"]
    elif old == ms.DONE:
        # Leaving done (a correction back to review) clears the approval stamps so a re-approval
        # re-stamps them honestly (#27).
        milestone.completed_at = None
        milestone.approved_by = None
        update_fields += ["completed_at", "approved_by"]
    milestone.save(update_fields=update_fields)
    record_activity(
        milestone.campaign, user, "milestone.status", target_kind="milestone",
        target_id=milestone.pk, before={"status": old}, after={"status": to_status},
    )
    if to_status == ms.DONE:
        resolve_dependencies_for(DependencyKind.MILESTONE, milestone.pk)
    recompute(milestone.campaign)
    _schedule_milestone_effects(milestone, to_status)
    return milestone


def _schedule_milestone_effects(milestone, to_status) -> None:
    """Post-commit review notification + calendar effect of a milestone status change
    (doc 04 §5, doc 09 §4/§5). ``ready_for_review`` nudges the commander; ``done``/``missed``
    retire the calendar deadline; other changes re-sync it for a live campaign."""
    ms = Milestone.MilestoneStatus
    campaign = milestone.campaign
    if to_status == ms.READY_FOR_REVIEW:
        transaction.on_commit(lambda: notify.approval_needed(campaign, milestone=milestone))
    if to_status in (ms.DONE, ms.MISSED):
        transaction.on_commit(lambda: cal.cancel_milestone(milestone))
    elif cal.campaign_publishes(campaign):
        transaction.on_commit(lambda: cal.publish_campaign(campaign))


def _milestone_ready_marker(milestone):
    """The pilot who most recently marked the milestone ready for review (the SoD counterpart)."""
    rows = (
        CampaignActivity.objects
        .filter(campaign_id=milestone.campaign_id, target_kind="milestone",
                target_id=milestone.pk, verb="milestone.status")
        .order_by("-created_at", "-id")
    )
    for row in rows:
        if (row.after or {}).get("status") == Milestone.MilestoneStatus.READY_FOR_REVIEW:
            return row.actor
    return None


@transaction.atomic
def resolve_dependency(dependency, user=None, reason=""):
    """Manually resolve a single dependency edge (the audited escape hatch, doc 04 §8).

    Auto edges resolve through :func:`resolve_dependencies_for` when their target reaches a
    terminal-done state; an ``external`` edge has no in-app target and a non-external edge may be
    cleared by hand — both require a mandatory ``reason``/note recorded on the activity row so the
    manual override is auditable (doc 04 §8 lines 243-246, #26). Idempotent: re-resolving an
    already-resolved edge is a no-op.
    """
    if dependency.is_resolved:
        return dependency
    if not (reason or "").strip():
        raise ValidationError("A reason is required to resolve a dependency by hand.")
    dependency.is_resolved = True
    dependency.save(update_fields=["is_resolved", "updated_at"])
    record_activity(
        dependency.campaign, user, "dependency.resolved", target_kind="dependency",
        target_id=dependency.pk, after={"is_resolved": True}, reason=(reason or "")[:300],
    )
    recompute(dependency.campaign)
    return dependency


# --------------------------------------------------------------------------- #
#  Linked operations (doc 06 §3.13, doc 10 line 216)
# --------------------------------------------------------------------------- #
@transaction.atomic
def link_operation(campaign, user, operation_id, note=""):
    """Soft-link an ``operations.Operation`` to a campaign (doc 10 line 216).

    The operations app stays unaware it is referenced (a bare ``operation_id``, ADR-0006), so the
    render layer resolves the id defensively. Idempotent via the unique ``(campaign, operation_id)``
    constraint — re-linking the same op returns the existing row. Manage-gated in the view."""
    from .models import CampaignOperation

    try:
        operation_id = int(operation_id)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Choose a valid operation to link.") from exc
    link, created = CampaignOperation.objects.get_or_create(
        campaign=campaign, operation_id=operation_id,
        defaults={"note": (note or "")[:200],
                  "added_by": user if getattr(user, "pk", None) else None},
    )
    if created:
        record_activity(
            campaign, user, "operation.linked", target_kind="campaign", target_id=campaign.pk,
            after={"operation_id": operation_id},
        )
    return link


@transaction.atomic
def unlink_operation(campaign, user, operation_id) -> bool:
    """Remove a campaign↔operation link (idempotent — unlinking a missing link is a no-op)."""
    from .models import CampaignOperation

    try:
        operation_id = int(operation_id)
    except (TypeError, ValueError):
        return False
    deleted, _ = CampaignOperation.objects.filter(
        campaign=campaign, operation_id=operation_id
    ).delete()
    if deleted:
        record_activity(
            campaign, user, "operation.unlinked", target_kind="campaign", target_id=campaign.pk,
            after={"operation_id": operation_id},
        )
    return bool(deleted)


# --------------------------------------------------------------------------- #
#  Volunteering (doc 10 §6.5, §6.6)
# --------------------------------------------------------------------------- #
def volunteer_for_objective(objective, user):
    """A pilot opts in to help with a ``help_wanted`` objective — creates a self-assigned linked
    task (a task they *chose*, doc 10 §6.6 tone rule) and records an activity row that surfaces
    in the objective owner's workspace queue. No pingboard alert: doc 09 defines no volunteer
    event, so per the 'never misuse a key' rule volunteering records activity only. Idempotent —
    a pilot who already has a live volunteer task for this objective just gets it back."""
    from apps.tasks.models import Task

    existing = objective.linked_tasks().filter(
        assignee=user, status__in=[Task.Status.OPEN, Task.Status.CLAIMED, Task.Status.IN_PROGRESS]
    ).first()
    if existing is not None:
        return existing
    task = create_objective_task(
        objective, user, title=f"Help: {objective.title}", assignee=user, due_at=objective.due_at,
    )
    record_activity(
        objective.campaign, user, "objective.volunteer", target_kind="objective",
        target_id=objective.pk, after={"task_id": task.pk},
    )
    return task


# --------------------------------------------------------------------------- #
#  Read services: pilot panel & officer workspace (doc 10 §6.5, §6.7)
# --------------------------------------------------------------------------- #
# Manual-metric confirmation interval for the workspace "stale" queue and the deadline sweep's
# nudge — read from ``campaigns.config → refresh.manual_stale_days`` (default 14) via
# ``_manual_stale_days()`` so leadership can retune it without a deploy (doc 08 §2.2).
_LIVE_OBJECTIVE_STATUSES = (Objective.ObjectiveStatus.PENDING, Objective.ObjectiveStatus.ACTIVE,
                            Objective.ObjectiveStatus.BLOCKED)


def _active_task_statuses():
    from apps.tasks.models import Task

    return [Task.Status.OPEN, Task.Status.CLAIMED, Task.Status.IN_PROGRESS]


def pilot_panel(user) -> dict:
    """Query-cheap Command-Center panel context (doc 10 §6.5): the pilot's visible active
    campaigns (with a why-it-matters snippet), their owned objectives, their live linked tasks,
    and help-wanted opportunities. Empty ``has_content`` ⇒ the panel is omitted, never rendered
    hollow."""
    from apps.tasks.models import Task

    vis = visible_campaigns(user)
    active = list(
        vis.filter(status=Campaign.Status.ACTIVE)
        .select_related("commander").order_by("-priority", "target_end_at", "-created_at")[:5]
    )
    my_objectives = list(
        Objective.objects.filter(owner=user, campaign__in=vis)
        .filter(status__in=_LIVE_OBJECTIVE_STATUSES)
        .select_related("campaign")[:5]
    )
    my_tasks = list(
        Task.objects.filter(assignee=user, related_type=Objective.RELATED_TYPE,
                            status__in=_active_task_statuses())
        .order_by("due_at")[:5]
    )
    help_wanted = list(
        Objective.objects.filter(help_wanted=True, campaign__in=vis,
                                 campaign__status=Campaign.Status.ACTIVE)
        .filter(status__in=_LIVE_OBJECTIVE_STATUSES)
        .exclude(owner=user)
        .select_related("campaign")[:5]
    )
    # A pilot always sees their own recognition — the private DM has a home on the dashboard, and
    # a personal feed is not gated by recognition_mode/public (doc 09 §7: recognition reaches the
    # recognised pilot regardless of their opt-out).
    recognition = list(
        CampaignRecognition.objects.filter(user=user, campaign__in=vis)
        .select_related("campaign", "awarded_by")[:5]
    )
    return {
        "campaigns": active,
        "my_objectives": my_objectives,
        "my_tasks": my_tasks,
        "help_wanted": help_wanted,
        "recognition": recognition,
        "has_content": bool(
            active or my_objectives or my_tasks or help_wanted or recognition
        ),
    }


def workspace_queues(user) -> dict:
    """Officer-workspace queues (doc 10 §6.7): everything of *mine* that needs a decision or an
    update, across the campaigns I own an objective in / lead a workstream in / command. Each
    queue is capped; the view renders them as htmx tabs."""
    now = timezone.now()
    uid = user.pk
    obj_status = Objective.ObjectiveStatus
    terminal = [obj_status.MET, obj_status.MISSED, obj_status.DROPPED]

    mine = (
        Objective.objects.filter(
            Q(owner_id=uid) | Q(campaign__commander_id=uid) | Q(workstream__lead_id=uid)
        )
        .select_related("campaign", "owner", "workstream").distinct()
    )
    live = mine.exclude(status__in=terminal)
    stale_cutoff = now - timezone.timedelta(days=_manual_stale_days())
    my_objective_ids = list(
        Objective.objects.filter(owner_id=uid).values_list("pk", flat=True)
    )
    volunteers = list(
        CampaignActivity.objects.filter(
            verb="objective.volunteer", target_kind="objective", target_id__in=my_objective_ids or [0],
        ).select_related("campaign", "actor").prefetch_related("actor__characters")
        .order_by("-created_at")[:50]
    )
    # Resolve each volunteered-on objective's title so the queue names it instead of "objective #<id>"
    # (the target_ids are all objectives this user owns). One query for the set, not per row.
    if volunteers:
        titles = dict(
            Objective.objects.filter(pk__in={a.target_id for a in volunteers})
            .values_list("pk", "title")
        )
        for a in volunteers:
            a.objective_title = titles.get(a.target_id)
    return {
        "my_objectives": list(live.order_by("due_at")[:50]),
        "overdue": list(live.filter(due_at__lt=now).order_by("due_at")[:50]),
        "blocked": list(live.filter(status=obj_status.BLOCKED)[:50]),
        "awaiting_verification": list(
            mine.filter(status=obj_status.MET, requires_verification=True,
                        verified_by__isnull=True)[:50]
        ),
        "stale_metrics": list(
            live.filter(metric_source="")
            .filter(Q(measured_at__lt=stale_cutoff) | Q(measured_at__isnull=True))[:50]
        ),
        "volunteers": volunteers,
        "my_workstreams": list(
            _workstreams_led(uid).select_related("campaign")[:50]
        ),
    }


def _workstreams_led(uid):
    return Workstream.objects.filter(lead_id=uid)


# --------------------------------------------------------------------------- #
#  Templates (doc 04 §13, doc 10 §6.10)
# --------------------------------------------------------------------------- #
def _strip_instance_params(params: dict) -> dict:
    """Drop instance-bound keys from a ``metric_params`` dict (blueprint safety, doc 04 §13)."""
    return {k: v for k, v in (params or {}).items() if k not in INSTANCE_PARAM_KEYS}


def _bp_dec(value):
    """Coerce a blueprint numeric (int/str/None) to ``Decimal`` or ``None``."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _offset_due(start_at, offset_days):
    """Materialise a blueprint day-offset into an absolute due date from the campaign start."""
    if start_at is None or offset_days is None:
        return None
    try:
        return start_at + timezone.timedelta(days=int(offset_days))
    except (TypeError, ValueError):
        return None


@transaction.atomic
def instantiate_template(template, user, *, name=None, start_at=None, target_end_at=None):
    """Materialise a :class:`CampaignTemplate` blueprint into a new ``draft`` campaign (doc 04 §13).

    Structure only is copied — workstreams, objectives, milestones and risks with their values;
    never a person, an absolute date, a measured value or an instance-bound metric id. Day-offsets
    resolve to absolute due dates only when a ``start_at`` is supplied; ``target_end_at`` falls
    back to ``start_at + window_days``. The campaign's creator becomes its commander by default and
    everything is fully editable afterwards — a template imposes nothing after creation. Children
    are written through the ORM with activity rows, then a single recompute settles progress/health.
    """
    bp = template.blueprint or {}
    creator = user if getattr(user, "pk", None) else None
    if start_at is not None and target_end_at is None and bp.get("window_days"):
        target_end_at = _offset_due(start_at, bp.get("window_days"))

    recognition_cfg = config.get("recognition")
    campaign = Campaign.objects.create(
        name=(name or template.name)[:120],
        summary=(bp.get("summary") or "")[:200],
        rationale=bp.get("rationale") or "",
        desired_outcome=bp.get("desired_outcome") or "",
        success_criteria=bp.get("success_criteria") or "",
        failure_criteria=bp.get("failure_criteria") or "",
        category=template.category or bp.get("category") or Campaign.Category.OTHER,
        status=Campaign.Status.DRAFT,
        visibility=Campaign.Visibility.OFFICERS,  # conservative default (doc 04 §13 D18)
        commander=creator,
        created_by=creator,
        start_at=start_at,
        target_end_at=target_end_at,
        recognition_mode=recognition_cfg.get("default_mode", "none"),
        recognition_public=bool(recognition_cfg.get("default_public", False)),
    )

    ws_by_key: dict = {}
    for w in bp.get("workstreams", []):
        ws = Workstream(
            campaign=campaign, name=(w.get("name") or "Lane")[:120],
            key=(w.get("key") or "")[:64], description=w.get("description") or "",
            sort_order=int(w.get("sort_order") or 0),
        )
        save_workstream(ws, user)  # derives a campaign-unique key + writes activity
        if w.get("key"):
            ws_by_key[w["key"]] = ws

    directions = set(Objective.Direction.values)
    for o in bp.get("objectives", []):
        ws = ws_by_key.get(o.get("workstream"))
        obj = Objective.objects.create(
            campaign=campaign, workstream=ws,
            title=(o.get("title") or "Objective")[:200], description=o.get("description") or "",
            unit=(o.get("unit") or "")[:16],
            direction=o.get("direction") if o.get("direction") in directions else Objective.Direction.GTE,
            weight=max(1, int(o.get("weight") or 1)),
            target_value=_bp_dec(o.get("target_value")), baseline_value=_bp_dec(o.get("baseline_value")),
            is_mandatory=bool(o.get("is_mandatory")),
            requires_verification=bool(o.get("requires_verification")),
            help_wanted=bool(o.get("help_wanted")), is_sensitive=bool(o.get("is_sensitive")),
            metric_source=(o.get("metric_source") or "")[:64],
            metric_params=_strip_instance_params(o.get("metric_params") or {}),
            due_at=_offset_due(start_at, o.get("due_offset_days")),
            sort_order=int(o.get("sort_order") or 0),
        )
        record_activity(
            campaign, user, "objective.created", target_kind="objective", target_id=obj.pk,
        )

    for m in bp.get("milestones", []):
        ms = Milestone.objects.create(
            campaign=campaign, workstream=ws_by_key.get(m.get("workstream")),
            title=(m.get("title") or "Milestone")[:200], description=m.get("description") or "",
            due_at=_offset_due(start_at, m.get("due_offset_days")),
            sort_order=int(m.get("sort_order") or 0),
        )
        record_activity(
            campaign, user, "milestone.saved", target_kind="milestone", target_id=ms.pk,
            after={"status": ms.status},
        )

    levels = set(Risk.RiskLevel.values)
    for r in bp.get("risks", []):
        risk = Risk(
            campaign=campaign, workstream=ws_by_key.get(r.get("workstream")),
            description=r.get("description") or "Risk",
            probability=r.get("probability") if r.get("probability") in levels else Risk.RiskLevel.MEDIUM,
            impact=r.get("impact") if r.get("impact") in levels else Risk.RiskLevel.MEDIUM,
            mitigation=r.get("mitigation") or "", contingency=r.get("contingency") or "",
            trigger=(r.get("trigger") or "")[:200],
        )
        risk.severity = compute_severity(risk.probability, risk.impact)
        risk.save()
        record_activity(
            campaign, user, "risk.saved", target_kind="risk", target_id=risk.pk,
            after={"severity": risk.severity, "status": risk.status},
        )

    record_activity(
        campaign, user, "campaign.created", target_kind="campaign", target_id=campaign.pk,
        after={"status": campaign.status, "template": template.key},
    )
    audit_log(
        user, "campaigns.created_from_template", target_type="campaign",
        target_id=str(campaign.pk), metadata={"template_key": template.key},
    )
    recompute(campaign)
    return campaign


def _campaign_to_blueprint(campaign) -> dict:
    """Distil a campaign's structure into a template blueprint (doc 04 §13 save-as-template).

    Strips every instance fact: people, current/measured values, evidence, activity, recognition,
    and instance-bound metric ids; converts absolute dates to day-offsets from the campaign start.
    Suggested defaults (target/baseline values, criteria prose) are retained. Dropped objectives
    are omitted."""
    start = campaign.start_at

    def offset(due):
        if not due or not start:
            return None
        return max(0, (due - start).days)

    ws_key_by_id: dict = {}
    workstreams = []
    for w in campaign.workstreams.order_by("sort_order", "id"):
        ws_key_by_id[w.pk] = w.key
        workstreams.append({
            "key": w.key, "name": w.name, "description": w.description, "sort_order": w.sort_order,
        })

    objectives = []
    for o in (campaign.objectives.exclude(status=Objective.ObjectiveStatus.DROPPED)
              .order_by("sort_order", "id")):
        objectives.append({
            "title": o.title, "description": o.description,
            "workstream": ws_key_by_id.get(o.workstream_id),
            "unit": o.unit, "direction": o.direction, "weight": o.weight,
            "target_value": _num(o.target_value), "baseline_value": _num(o.baseline_value),
            "is_mandatory": o.is_mandatory, "requires_verification": o.requires_verification,
            "help_wanted": o.help_wanted, "is_sensitive": o.is_sensitive,
            "metric_source": o.metric_source,
            "metric_params": _strip_instance_params(o.metric_params or {}),
            "due_offset_days": offset(o.due_at), "sort_order": o.sort_order,
        })

    milestones = [
        {"title": m.title, "description": m.description,
         "workstream": ws_key_by_id.get(m.workstream_id),
         "due_offset_days": offset(m.due_at), "sort_order": m.sort_order}
        for m in campaign.milestones.order_by("sort_order", "id")
    ]

    risks = [
        {"description": r.description, "workstream": ws_key_by_id.get(r.workstream_id),
         "probability": r.probability, "impact": r.impact, "mitigation": r.mitigation,
         "contingency": r.contingency, "trigger": r.trigger}
        for r in campaign.risks.exclude(status=Risk.RiskStatus.RETIRED).order_by("id")
    ]

    return {
        "category": campaign.category,
        "window_days": offset(campaign.target_end_at),
        "summary": campaign.summary, "rationale": campaign.rationale,
        "desired_outcome": campaign.desired_outcome,
        "success_criteria": campaign.success_criteria, "failure_criteria": campaign.failure_criteria,
        "workstreams": workstreams, "objectives": objectives,
        "milestones": milestones, "risks": risks,
    }


@transaction.atomic
def save_as_template(campaign, user, *, key, name, description=""):
    """Save a campaign's structure as a reusable custom template (doc 04 §13, doc 10 §6.8 step 8).

    Manage-gated in the view. The ``key`` must be unique and must not collide with a builtin —
    builtins are clone-to-custom only, never overwritten. Audited (``campaigns.template_saved``)."""
    key = slugify(key or name)[:64]
    if not key:
        raise ValidationError("A template needs a name to derive its key from.")
    if CampaignTemplate.objects.filter(key=key).exists():
        raise ValidationError("A template with that key already exists — choose a different name.")
    template = CampaignTemplate.objects.create(
        key=key, name=(name or campaign.name)[:120], description=(description or "").strip(),
        category=campaign.category, blueprint=_campaign_to_blueprint(campaign),
        is_builtin=False, created_from=campaign,
        created_by=user if getattr(user, "pk", None) else None, active=True,
    )
    record_activity(
        campaign, user, "template.saved", target_kind="campaign", target_id=campaign.pk,
        after={"template_key": key},
    )
    audit_log(
        user, "campaigns.template_saved", target_type="campaign", target_id=str(campaign.pk),
        metadata={"template_key": key, "name": template.name},
    )
    return template


# --------------------------------------------------------------------------- #
#  Recognition & participation (doc 04 §12, doc 11 §2.4)
# --------------------------------------------------------------------------- #
@transaction.atomic
def award_recognition(campaign, user, awarded_by, *, category, points=0, reason):
    """Record a manual, audited recognition entry for a pilot (doc 04 §12, brief §3).

    ``reason`` is mandatory and non-empty; a self-award is blocked unless the awarder is a director
    (separation of duties, the raffle ``grant_manual_tickets`` pattern). Entries are append-only —
    a correction is a compensating negative-``points`` entry, never an edit. Writes activity +
    ``audit_log("campaigns.recognition_adjusted")`` and DMs the recognised pilot (notify)."""
    if not (reason or "").strip():
        raise ValidationError("A reason is required to record recognition.")
    uid = getattr(user, "pk", None)
    if uid is None:
        raise ValidationError("Recognition needs a pilot to recognise.")
    if (getattr(awarded_by, "pk", None) == uid and not has_role(awarded_by, ROLE_DIRECTOR)):
        raise ValidationError(
            "You can't record recognition for your own account unless you are a director."
        )
    try:
        points = int(points)
    except (TypeError, ValueError):
        points = 0

    row = CampaignRecognition.objects.create(
        campaign=campaign, user=user,
        category=(category or "").strip()[:64] or "contribution",
        points=points, reason=reason.strip()[:300],
        awarded_by=awarded_by if getattr(awarded_by, "pk", None) else None,
    )
    record_activity(
        campaign, awarded_by, "recognition.awarded", target_kind="recognition", target_id=row.pk,
        after={"user_id": uid, "points": points}, reason=reason.strip()[:300],
    )
    audit_log(
        awarded_by, "campaigns.recognition_adjusted", target_type="campaign",
        target_id=str(campaign.pk),
        metadata={"recognition_id": row.pk, "user_id": uid, "points": points,
                  "category": row.category, "reason": reason.strip()[:120]},
    )
    # Bust after commit so a concurrent panel read can't re-cache the pre-commit snapshot for the
    # full 300 s TTL (#34); notify is likewise post-commit.
    transaction.on_commit(lambda: bust_participation(campaign))
    transaction.on_commit(lambda: notify.recognition(row))
    return row


def participation_cache_key(campaign) -> str:
    return f"campaigns:participation:{campaign.pk}:v1"


def bust_participation(campaign) -> None:
    """Drop the per-campaign participation aggregate (doc 11 §2.4 explicit-bust backstop)."""
    from django.core.cache import cache

    cache.delete(participation_cache_key(campaign))


def _participation_aggregate(campaign) -> dict:
    """Viewer-independent per-user participation aggregate, cached 300 s (doc 11 §2.4).

    Two viewer-independent maps keyed by ``user_id``: ``contrib`` (derived contribution count +
    weighted points from campaign-linked task completions in ``pilots.ContributionEvent``) and
    ``recognitions`` (manual ``CampaignRecognition`` rows). Redaction is applied per request on top
    of this, so the cache never has to know who is looking."""
    from django.core.cache import cache

    ck = participation_cache_key(campaign)
    cached = cache.get(ck)
    if cached is not None:
        return cached

    from apps.pilots.models import ContributionEvent

    # One campaign-level clause plus one objective prefix, then scope to this campaign's objective
    # ids in Python — instead of a 2N+1-term OR over the (unindexed) gap_ref (doc 11 §2.4, #22).
    obj_ids = set(campaign.objectives.values_list("pk", flat=True))
    prefix = f"{Objective.RELATED_TYPE}:"
    q = Q(gap_ref=f"campaign:{campaign.pk}") | Q(gap_ref__startswith=prefix)

    contrib: dict = {}
    for ev in ContributionEvent.objects.filter(q).values("user_id", "points", "gap_ref"):
        gap = ev["gap_ref"]
        if gap.startswith(prefix):
            head = gap[len(prefix):].split(":", 1)[0]
            if not head.isdigit() or int(head) not in obj_ids:
                continue  # an objective link belonging to another campaign
        rec = contrib.setdefault(ev["user_id"], {"count": 0, "points": 0})
        rec["count"] += 1
        rec["points"] += int(ev["points"] or 0)

    recognitions: dict = {}
    for r in (
        campaign.recognitions.select_related("awarded_by")
        .prefetch_related("awarded_by__characters").order_by("created_at", "id")
    ):
        recognitions.setdefault(r.user_id, []).append({
            "points": r.points, "reason": r.reason, "category": r.category,
            # The awarder's friendly (main-character) name, not the opaque ``eve:<id>`` username —
            # the aggregate is cached, so this resolves once per 300 s window (doc 11 §2.4).
            "awarded_by": (r.awarded_by.display_name if r.awarded_by_id else ""),
            "at": r.created_at.isoformat(),
        })

    data = {"contrib": contrib, "recognitions": recognitions}
    cache.set(ck, data, 300)
    return data


def _participation_rule_text(mode) -> str:
    """The prose counting rule shown on the panel — no opaque scores (req §5 contributions)."""
    if mode == Campaign.RecognitionMode.POINTS:
        return ("Points come from completed campaign-linked tasks (leadership-weighted) plus any "
                "manual recognition awards. Manual awards show their reason and who gave them.")
    return ("Counts are completed campaign-linked tasks credited to each pilot, plus any manual "
            "recognition. Manual awards show their reason and who gave them.")


def participation_panel(campaign, user) -> dict:
    """Read-time participation view honouring ``recognition_mode``/``recognition_public`` and each
    pilot's opt-out (doc 04 §12, doc 11 §2.4).

    ``none`` mode renders nothing. Directors, the commander and manage-capable viewers see every
    contributor named. Otherwise: a pilot sees their own line always; on a *public* campaign they
    also see other non-opted-out pilots (opted-out pilots fold into an "N other contributors"
    line); on a non-public campaign non-leaders see only their own line (others are not revealed)."""
    mode = campaign.recognition_mode
    if mode == Campaign.RecognitionMode.NONE:
        return {"mode": "none", "has_content": False}

    agg = _participation_aggregate(campaign)
    all_uids = set(agg["contrib"]) | set(agg["recognitions"])
    if not all_uids:
        # Nothing to attribute yet — skip the preference/user lookups entirely (a campaign with no
        # contributions or awards renders the empty state, doc 11 §2.4).
        return {"mode": mode, "public": campaign.recognition_public, "rows": [],
                "other_count": 0, "rule_text": _participation_rule_text(mode), "has_content": False}
    uid = getattr(user, "pk", None)
    is_leader = has_role(user, ROLE_DIRECTOR) or can_manage(user, campaign)
    public = campaign.recognition_public

    from django.contrib.auth import get_user_model

    from apps.pilots.models import PilotPreference

    opted_out = set(
        PilotPreference.objects.filter(user_id__in=all_uids or [0], public_recognition=False)
        .values_list("user_id", flat=True)
    )
    users = {
        u.pk: u
        for u in get_user_model().objects.filter(pk__in=all_uids or [0]).prefetch_related("characters")
    }

    rows = []
    other_count = 0
    for target_uid in all_uids:
        is_self = target_uid == uid
        if is_leader:
            named = True
        elif public:
            named = is_self or target_uid not in opted_out
        else:
            named = is_self
        if not named:
            if public:  # only a public campaign reveals that other contributors exist
                other_count += 1
            continue
        contrib = agg["contrib"].get(target_uid, {"count": 0, "points": 0})
        recs = agg["recognitions"].get(target_uid, [])
        rec_points = sum(int(r["points"]) for r in recs)
        rows.append({
            "user": users.get(target_uid),
            "count": contrib["count"],
            "points": contrib["points"] + rec_points,
            "recognitions": recs,
        })

    rows.sort(key=lambda r: (r["points"], r["count"]), reverse=True)
    return {
        "mode": mode,
        "public": public,
        "rows": rows,
        "other_count": other_count,
        "rule_text": _participation_rule_text(mode),
        "has_content": bool(rows or other_count),
    }


# --------------------------------------------------------------------------- #
#  Close-out (doc 04 §11, doc 10 §6.8)
# --------------------------------------------------------------------------- #
_CLOSE_STATUSES = (Campaign.Status.COMPLETED, Campaign.Status.FAILED, Campaign.Status.CANCELLED)


@transaction.atomic
def close_campaign(campaign, user, *, final_status, reason="", resolutions=None,
                   manual_values=None, outcome_summary="", lessons_learned="",
                   spent_isk=None, budget_allowed=False, followup_objective_ids=None,
                   recognitions=None, save_template=None):
    """The guided close-out, applied as one server-validated transaction (doc 04 §11).

    Order: confirm final-value corrections and per-objective resolutions → record outcome/lessons
    (and any budget correction) → spawn follow-up tasks for unfinished objectives → record
    recognition → optionally save-as-template → execute the T7/T8/T9 transition (which stamps
    ``closed_by``/``closed_at``, emits the completion broadcast and cancels future calendar events).
    Completing with open mandatory objectives still demands the director override reason enforced in
    :func:`set_status`. Any validation failure rolls the whole thing back — nothing partial."""
    if final_status not in _CLOSE_STATUSES:
        raise ValidationError("Choose a final status of completed, failed or cancelled.")
    # Lock the campaign row up front and validate ACTIVE on the *locked* row, so two overlapping
    # close POSTs (a double-click under multi-worker) can't both pass — the second finds the row
    # already terminal and loses cleanly (doc 07 T13, #15).
    locked = Campaign.objects.select_for_update().get(pk=campaign.pk)
    if locked.status != Campaign.Status.ACTIVE:
        raise ValidationError("Only an active campaign can be closed — resume a paused campaign first.")
    if not (outcome_summary or "").strip():
        raise ValidationError("Record an outcome summary before closing.")
    if final_status in (Campaign.Status.COMPLETED, Campaign.Status.FAILED) \
            and not (lessons_learned or "").strip():
        raise ValidationError("Lessons learned are required when completing or failing a campaign.")

    # 1 · final-value corrections for manual objectives (before resolution so progress is current).
    for obj_id, (value, note) in (manual_values or {}).items():
        obj = campaign.objectives.filter(pk=obj_id).first()
        if obj is not None and not obj.metric_source and value not in (None, ""):
            update_manual_value(obj, user, value, note or "Confirmed at close-out")

    # 2 · per-objective resolution to a terminal status.
    for obj_id, spec in (resolutions or {}).items():
        obj = campaign.objectives.filter(pk=obj_id).first()
        if obj is None:
            continue
        to_status = spec.get("status")
        if not to_status or obj.status == to_status:
            continue
        set_objective_status(obj, user, to_status, reason=spec.get("note", ""))

    # 2b · every objective must now be terminal (doc 04 §11 step 2, #8). A director may force-close
    #      past unresolved objectives with a reason — the same break-glass set_status honours for T7.
    terminal = {Objective.ObjectiveStatus.MET, Objective.ObjectiveStatus.MISSED,
                Objective.ObjectiveStatus.DROPPED}
    unresolved = [o.pk for o in campaign.objectives.all() if o.status not in terminal]
    if unresolved and not (has_role(user, ROLE_DIRECTOR) and (reason or "").strip()):
        raise ValidationError(
            "Resolve every objective to met, missed or dropped before closing (unresolved: "
            + ", ".join(str(pk) for pk in unresolved) + ")."
        )

    # 3 · outcome, lessons, and any budget correction.
    campaign.outcome_summary = (outcome_summary or "").strip()
    campaign.lessons_learned = (lessons_learned or "").strip()
    update_fields = ["outcome_summary", "lessons_learned", "updated_at"]
    if budget_allowed and spent_isk is not None:
        try:
            new_spent = Decimal(str(spent_isk))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValidationError("Enter a valid spent figure.") from exc
        if new_spent < 0:
            raise ValidationError("Spent ISK cannot be negative.")
        if new_spent != (campaign.spent_isk or Decimal(0)):
            campaign.spent_isk = new_spent
            update_fields.append("spent_isk")
            # Budget is director + commander only, so the raw figure is redacted in the pull-based
            # activity feed; the real value lives in the director-only audit below (doc 07 T10, #30).
            record_activity(
                campaign, user, "budget.changed", target_kind="campaign", target_id=campaign.pk,
                before={"spent_isk": _REDACTED}, after={"spent_isk": _REDACTED},
            )
            audit_log(
                user, "campaigns.budget_changed", target_type="campaign",
                target_id=str(campaign.pk), metadata={"spent_isk": _num(new_spent)},
            )
    campaign.save(update_fields=update_fields)

    # 4 · unfinished-work follow-ups: one linked task per flagged open objective (doc 04 §11 step 5).
    followups = []
    for obj_id in (followup_objective_ids or []):
        obj = campaign.objectives.filter(pk=obj_id).first()
        if obj is None:
            continue
        task = create_objective_task(obj, user, title=f"Follow-up: {obj.title}"[:200])
        followups.append(task)

    # 5 · recognition entries recorded at close-out (self-award SoD enforced in award_recognition).
    for spec in (recognitions or []):
        target = spec.get("user")
        if target is None:
            continue
        award_recognition(
            campaign, target, user, category=spec.get("category", ""),
            points=spec.get("points", 0), reason=spec.get("reason", ""),
        )

    # 6 · optional save-as-template.
    if save_template:
        save_as_template(
            campaign, user, key=save_template.get("key", ""),
            name=save_template.get("name", ""), description=save_template.get("description", ""),
        )

    # 7 · execute the terminal transition (stamps closure, notifies, cancels calendar).
    set_status(campaign, final_status, user, reason=reason, via_closeout=True)
    # 8 · archive automatically as the final step — a closed campaign is read-only forever
    #     (doc 04 T11, §11, #9).
    set_status(campaign, Campaign.Status.ARCHIVED, user)
    return {"campaign": campaign, "followups": followups}
