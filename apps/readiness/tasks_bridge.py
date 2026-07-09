"""Bridge a ReadinessFinding into an actionable ``tasks.Task`` (design doc 12).

A finding describes *what is wrong*; a Task is *the work to fix it*. This mirrors
``apps/operations/views.py::op_generate_tasks`` exactly — same ``related_type`` /
``related_id`` dedupe key and skip-if-active guard — reusing the task backbone with
no new model or status. Used by both creation paths: the manual "Create task"
button and the ``readiness.generate_tasks`` beat.
"""
from __future__ import annotations

RELATED_TYPE = "readiness"

# severity → priority bump (doc 12 §2); higher Task.priority = more urgent.
_SEVERITY_BUMP = {"info": 0, "warn": 5, "high": 10, "critical": 20}
SEVERITY_RANK = {"info": 0, "warn": 1, "high": 2, "critical": 3}


def resolve_assignee(owner_tag: str, responsibilities: dict):
    """First mapped user for an owner tag, or ``None`` (open/claimable pool)."""
    if not owner_tag:
        return None
    users = ((responsibilities.get("owner_tags") or {}).get(owner_tag) or {}).get("users") or []
    if not users:
        return None
    from django.contrib.auth import get_user_model

    return get_user_model().objects.filter(pk=users[0]).first()


def active_task_exists(finding) -> bool:
    """The §3 dedupe guard: an open/claimed/in-progress task already covers it."""
    from apps.tasks.models import Task

    return Task.objects.filter(
        related_type=RELATED_TYPE, related_id=str(finding.id),
        status__in=[Task.Status.OPEN, Task.Status.CLAIMED, Task.Status.IN_PROGRESS],
    ).exists()


def task_for_finding(finding, *, user=None):
    """Create a Task from a finding and back-link it (``finding.task``). Returns the Task.

    The caller is responsible for the active-task guard (``active_task_exists``) so
    this never double-creates against the dedupe key.
    """
    from apps.tasks.models import Task

    from . import config as config_module

    task_type = finding.task_type if finding.task_type in Task.Type.values else Task.Type.OTHER
    priority = round(finding.weight) + _SEVERITY_BUMP.get(finding.severity, 0)
    assignee = resolve_assignee(finding.owner_tag, config_module.get("responsibilities"))
    due_at = finding.predicted_breach_at if finding.kind == finding.Kind.FORECAST else None

    task = Task.objects.create(
        type=task_type,
        title=(finding.task_title or finding.title)[:200],
        description=finding.detail,
        priority=priority,
        status=Task.Status.OPEN,
        is_open=True,
        assignee=assignee,
        related_type=RELATED_TYPE,
        related_id=str(finding.id),
        created_by=user,
        due_at=due_at,
    )
    finding.task = task
    finding.save(update_fields=["task"])
    return task
