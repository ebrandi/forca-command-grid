"""Task lifecycle: claim, progress, complete — with audited transitions.

Completing a task credits the doer through the contribution ledger, and (when
the task links to a doctrine/operation/etc.) is where future side-effects on the
linked entity will hook in.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.pilots.services import record_contribution

from .models import Task, TaskEvent


def _record_event(task: Task, actor, from_status: str, to_status: str, note: str = "") -> None:
    TaskEvent.objects.create(
        task=task, actor=actor, from_status=from_status, to_status=to_status, note=note
    )


def can_act(user, task: Task, *, is_officer: bool) -> bool:
    """Only the assignee or an officer may change a task's state."""
    return is_officer or (task.assignee_id == user.id)


# SDE-2 (3.7): a real transition graph — illogical moves (e.g. done→open, cancelled→in
# progress) are blocked; DONE and CANCELLED are terminal.
_ALLOWED_TRANSITIONS = {
    # OPEN → DONE is allowed: an officer can directly close an unclaimed task they did ad-hoc.
    Task.Status.OPEN: {
        Task.Status.CLAIMED, Task.Status.IN_PROGRESS, Task.Status.DONE, Task.Status.CANCELLED,
    },
    Task.Status.CLAIMED: {
        Task.Status.OPEN, Task.Status.IN_PROGRESS, Task.Status.DONE, Task.Status.CANCELLED,
    },
    Task.Status.IN_PROGRESS: {
        Task.Status.OPEN, Task.Status.CLAIMED, Task.Status.DONE, Task.Status.CANCELLED,
    },
    Task.Status.DONE: set(),
    Task.Status.CANCELLED: set(),
}


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in _ALLOWED_TRANSITIONS.get(from_status, set())


@transaction.atomic
def claim(task: Task, user) -> bool:
    """A member claims an open, unassigned task. Returns False if not claimable."""
    locked = Task.objects.select_for_update().get(pk=task.pk)
    if not locked.is_open or locked.assignee_id or not locked.is_active:
        return False
    prev = locked.status
    locked.assignee = user
    locked.is_open = False
    locked.status = Task.Status.CLAIMED
    locked.save(update_fields=["assignee", "is_open", "status", "updated_at"])
    _record_event(locked, user, prev, locked.status)
    return True


@transaction.atomic
def set_status(task: Task, user, to_status: str) -> bool:
    """Transition a task. Completing it credits the doer (idempotent)."""
    locked = Task.objects.select_for_update().get(pk=task.pk)
    if to_status not in Task.Status.values or to_status == locked.status:
        return False
    if not can_transition(locked.status, to_status):
        return False  # illogical transition (e.g. done→open) — blocked
    prev = locked.status
    locked.status = to_status
    # Returning a task to the open pool clears the assignee so it's claimable again.
    if to_status == Task.Status.OPEN:
        locked.assignee = None
        locked.is_open = True
        locked.save(update_fields=["status", "assignee", "is_open", "updated_at"])
    else:
        locked.save(update_fields=["status", "updated_at"])
    _record_event(locked, user, prev, to_status)

    if to_status == Task.Status.DONE:
        # SDE-2 (3.7): credit the ACTOR — an officer closing a null-assignee task from the
        # "all" view now credits themselves, not nobody.
        record_contribution(
            locked.assignee or user,
            kind="task",
            magnitude=1,
            unit="tasks",
            description=locked.title,
            ref_type="task",
            ref_id=str(locked.pk),
            gap_ref=f"{locked.related_type}:{locked.related_id}" if locked.related_id else "",
            occurred_at=timezone.now(),
        )
    return True


@transaction.atomic
def edit_task(task: Task, user, *, title: str, priority: int, due_at) -> bool:
    """SDE-2 (3.7): edit a task's title / priority / due date (only while active), audited."""
    locked = Task.objects.select_for_update().get(pk=task.pk)
    if not locked.is_active:
        return False  # a done/cancelled task is frozen
    changed = []
    if title and title[:200] != locked.title:
        locked.title = title[:200]
        changed.append("title")
    if int(priority) != locked.priority:
        locked.priority = int(priority)
        changed.append("priority")
    if due_at != locked.due_at:
        locked.due_at = due_at
        changed.append("due date")
    if not changed:
        return False
    locked.save(update_fields=["title", "priority", "due_at", "updated_at"])
    _record_event(locked, user, locked.status, locked.status, note="Edited " + ", ".join(changed))
    return True


@transaction.atomic
def reassign(task: Task, user, new_assignee) -> bool:
    """SDE-2 (3.7): reassign (or unassign) a task, keeping status coherent, audited."""
    locked = Task.objects.select_for_update().get(pk=task.pk)
    if not locked.is_active or new_assignee == locked.assignee:
        return False
    prev = locked.status
    if new_assignee is None:  # back to the open pool
        locked.assignee = None
        locked.is_open = True
        # Any active claimed/in-progress task returns to OPEN so it's claimable again —
        # otherwise an unassigned IN_PROGRESS task would be orphaned out of the pool.
        if locked.status in (Task.Status.CLAIMED, Task.Status.IN_PROGRESS):
            locked.status = Task.Status.OPEN
        note = "Unassigned"
    else:
        locked.assignee = new_assignee
        locked.is_open = False
        if locked.status == Task.Status.OPEN:
            locked.status = Task.Status.CLAIMED
        note = f"Reassigned to {new_assignee.get_username()}"
    locked.save(update_fields=["assignee", "is_open", "status", "updated_at"])
    _record_event(locked, user, prev, locked.status, note=note)
    return True


# --- shared programmatic task factory (design ADR-0006) ----------------------
_ACTIVE_STATUSES = [Task.Status.OPEN, Task.Status.CLAIMED, Task.Status.IN_PROGRESS]


def active_task_exists(related_type: str, related_id) -> bool:
    """Dedupe guard: an open/claimed/in-progress task already covers this ref."""
    return Task.objects.filter(
        related_type=related_type, related_id=str(related_id), status__in=_ACTIVE_STATUSES
    ).exists()


def create_task(
    *,
    task_type: str,
    title: str,
    description: str = "",
    priority: int = 0,
    assignee=None,
    due_at=None,
    related_type: str,
    related_id,
    created_by=None,
) -> Task:
    """Idempotent task creation with the established related_type/related_id dedupe.

    The shared factory apps should use instead of inline ``Task.objects.create(...)``
    (readiness/operations/doctrines all re-implement this). If an active task already
    covers the ref it is returned rather than forking a duplicate. An ``assignee``
    makes the task CLAIMED; otherwise it is OPEN and claimable from the pool.
    """
    existing = Task.objects.filter(
        related_type=related_type, related_id=str(related_id), status__in=_ACTIVE_STATUSES
    ).first()
    if existing:
        return existing
    resolved_type = task_type if task_type in Task.Type.values else Task.Type.OTHER
    return Task.objects.create(
        type=resolved_type,
        title=str(title)[:200],
        description=description or "",
        priority=priority,
        status=Task.Status.CLAIMED if assignee else Task.Status.OPEN,
        is_open=assignee is None,
        assignee=assignee,
        due_at=due_at,
        related_type=related_type,
        related_id=str(related_id),
        created_by=created_by,
    )
