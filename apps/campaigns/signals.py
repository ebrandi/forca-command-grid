"""Signal wiring for Campaign Command (connected once at ``AppConfig.ready()``).

Closes the linked-task loop (doc 04 §10, doc 08 §5): a ``tasks.Task`` soft-linked to a campaign
objective (``related_type="campaign_objective"``) records its terminal transitions on the
objective's campaign as **automation** activity (``actor=None``), and when the objective's last
active linked task reaches ``done`` it records a roll-up row the owner sees in the objective
history and workspace.

Deliberately there is **no** objective status auto-change: a human closes the objective (doc 04
§14 "task completion only ever adds evidence"; the requirements forbid silent completion). And
there is no pingboard alert for the roll-up — doc 09 defines no task-completion event, and the
'never invent/misuse a registry key' rule means the nudge is the pull-based activity row, in
keeping with doc 09 §6 (dashboards are the system of record; notifications are only a nudge).
"""
from __future__ import annotations


def connect() -> None:
    from django.db.models.signals import post_save, pre_save

    from apps.tasks.models import Task

    pre_save.connect(_on_task_presave, sender=Task, dispatch_uid="campaigns_task_presave")
    post_save.connect(_on_task_saved, sender=Task, dispatch_uid="campaigns_task_rollup")


def _on_task_presave(sender, instance, **kwargs) -> None:
    """Snapshot the persisted status onto the instance so the post-save handler can tell a real
    terminal transition from a no-op re-save. Without this an admin title edit on an already-done
    linked task would re-fire the completion activity + roll-up on every save (#31)."""
    from apps.tasks.models import Task

    from .models import Objective

    if instance.related_type != Objective.RELATED_TYPE:
        return
    if instance.pk is None:
        instance._campaigns_prev_status = None
        return
    instance._campaigns_prev_status = (
        Task.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    )


def _on_task_saved(sender, instance, created, **kwargs) -> None:
    from django.db import transaction

    from apps.tasks.models import Task

    from . import services
    from .models import Objective

    if instance.related_type != Objective.RELATED_TYPE:
        return
    # Only terminal transitions are interesting; claims/edits/progress are the task board's job.
    if instance.status not in (Task.Status.DONE, Task.Status.CANCELLED):
        return
    # A re-save of an already-terminal task (e.g. an admin title fix) is not a transition and must
    # not append duplicate rows (#31); only a genuine move into the terminal status counts.
    prev_status = getattr(instance, "_campaigns_prev_status", None)
    if not created and prev_status == instance.status:
        return

    objective = (
        Objective.objects.filter(pk=str(instance.related_id).split(":")[0])
        .select_related("campaign").first()
    )
    if objective is None:  # dangling soft link (objective deleted) — inert by design
        return

    services.record_activity(
        objective.campaign, None, f"objective.task_{instance.status}",
        target_kind="objective", target_id=objective.pk,
        after={"task_id": instance.pk, "status": instance.status}, source="automation",
    )

    if instance.status == Task.Status.DONE:
        # A completed campaign-linked task adds a derived contribution (pilots.ContributionEvent);
        # drop the participation aggregate after commit so a concurrent panel read can't re-cache
        # the pre-commit snapshot (doc 11 §2.4 explicit bust, #34).
        campaign = objective.campaign
        transaction.on_commit(lambda: services.bust_participation(campaign))
        # Roll-up: evaluate "all linked tasks done" after commit so two last-active tasks completing
        # in overlapping transactions each see the other's committed state — neither loses the
        # marker under READ COMMITTED (#32).
        transaction.on_commit(lambda oid=objective.pk: _rollup_tasks_done(oid))


def _rollup_tasks_done(objective_id) -> None:
    """After commit: if the objective has no active linked tasks left and at least one is done,
    record the owner-facing roll-up marker the owner sees in the objective history / workspace. No
    status change (a human closes the objective — automation never does)."""
    from apps.tasks.models import Task

    from . import services
    from .models import Objective

    objective = (
        Objective.objects.filter(pk=objective_id).select_related("campaign").first()
    )
    if objective is None:
        return
    linked = objective.linked_tasks()
    active = linked.filter(
        status__in=[Task.Status.OPEN, Task.Status.CLAIMED, Task.Status.IN_PROGRESS]
    )
    if not active.exists() and linked.filter(status=Task.Status.DONE).exists():
        services.record_activity(
            objective.campaign, None, "objective.tasks_done",
            target_kind="objective", target_id=objective.pk,
            after={"owner_id": objective.owner_id}, source="automation",
        )
