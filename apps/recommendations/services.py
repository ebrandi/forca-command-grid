"""Recommendation services: action queue and closing the loop."""
from __future__ import annotations

import math

from django.utils import timezone

from core.audit import audit_log

from .models import ActionQueueItem, Recommendation

_CONFIDENCE_WEIGHT = {
    Recommendation.Confidence.HIGH: 1.0,
    Recommendation.Confidence.MEDIUM: 0.6,
    Recommendation.Confidence.LOW: 0.3,
}


def composite_score(rec: Recommendation) -> float:
    """Composite action-queue rank = severity × confidence × ISK impact (ROADMAP
    §7), replacing the severity-only sort. Confidence is a label, so it maps to a
    weight; ISK gives a gentle log-scaled multiplicative boost so a high-value rec
    edges out an equal-severity low-value one without swamping severity/confidence."""
    conf = _CONFIDENCE_WEIGHT.get(rec.confidence, 0.6)
    isk = float(rec.isk_impact or 0)
    isk_boost = min(1.0, math.log10(1 + isk / 1_000_000) / 4) if isk > 0 else 0.0
    return rec.severity * conf * (1 + isk_boost)


def set_action_links(rec: Recommendation, user, *, project_id=None, haul_task_id=None, ip: str = "") -> None:
    """Link an officer recommendation's action-queue item to a project / haul task
    (REC-5). Creates the item if the rec never got one — build_action_queue only
    makes items for NEW recs, so an ACKNOWLEDGED rec may have none yet."""
    item, _ = ActionQueueItem.objects.get_or_create(recommendation=rec)
    item.linked_project_id = project_id
    item.linked_hauling_task_id = haul_task_id
    item.save(update_fields=["linked_project_id", "linked_hauling_task_id"])
    audit_log(
        user, "recommendation.link", target_type="recommendation", target_id=str(rec.id),
        metadata={"project": project_id, "haul": haul_task_id}, ip=ip,
    )

_ACTION_STATES = {
    "acknowledge": Recommendation.State.ACKNOWLEDGED,
    "action": Recommendation.State.ACTIONED,
    "dismiss": Recommendation.State.DISMISSED,
}
_QUEUE_STATES = {
    "acknowledge": ActionQueueItem.Status.IN_PROGRESS,
    "action": ActionQueueItem.Status.DONE,
    "dismiss": ActionQueueItem.Status.DISMISSED,
}


def build_action_queue() -> int:
    """Ensure an action-queue item exists for each open officer recommendation."""
    created = 0
    for rec in Recommendation.objects.filter(
        state=Recommendation.State.NEW, required_permission__in=["officer", "director"]
    ):
        _, was_created = ActionQueueItem.objects.get_or_create(recommendation=rec)
        created += 1 if was_created else 0
    return created


_OPEN_STATES = {Recommendation.State.NEW, Recommendation.State.ACKNOWLEDGED}


def act_on_recommendation(rec: Recommendation, user, action: str, ip: str = "") -> None:
    """Acknowledge / action / dismiss an OPEN recommendation (audit-logged)."""
    if action not in _ACTION_STATES:
        raise ValueError(f"unknown action {action!r}")
    if rec.state not in _OPEN_STATES:
        # Don't reopen a closed/superseded recommendation.
        raise ValueError(f"recommendation is not open (state={rec.state})")

    rec.state = _ACTION_STATES[action]
    update_fields = ["state"]
    if action in ("action", "dismiss"):
        rec.closed_at = timezone.now()
        rec.closed_by = user
        update_fields += ["closed_at", "closed_by"]
    rec.save(update_fields=update_fields)
    rec.action_items.update(status=_QUEUE_STATES[action], assigned_to=user)
    audit_log(
        user,
        f"recommendation.{action}",
        target_type="recommendation",
        target_id=str(rec.id),
        metadata={"type": rec.type},
        ip=ip,
    )
