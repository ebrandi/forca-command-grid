"""Signal wiring for Command Intelligence.

Connected once at ``AppConfig.ready()``. Closes the COA→task loop (doc 07 §6, doc 10
§3): when a COA's last active task reaches ``done`` the COA moves
``in_progress→completed`` and an outcome measurement is enqueued; if a task is
cancelled the COA returns to ``accepted`` (re-actionable).
"""
from __future__ import annotations


def connect() -> None:
    from django.db.models.signals import post_save

    from apps.tasks.models import Task

    post_save.connect(_on_task_saved, sender=Task, dispatch_uid="command_intel_task_coa_loop")


def _on_task_saved(sender, instance, **kwargs) -> None:
    from apps.tasks.models import Task

    from .models import CourseOfAction

    if instance.related_type != CourseOfAction.RELATED_TYPE:
        return
    coa = CourseOfAction.objects.filter(pk=instance.related_id).first()
    if coa is None or coa.state != CourseOfAction.State.IN_PROGRESS:
        return

    active = coa.linked_tasks().filter(
        status__in=[Task.Status.OPEN, Task.Status.CLAIMED, Task.Status.IN_PROGRESS]
    )
    if instance.status == Task.Status.DONE and not active.exists():
        coa.state = CourseOfAction.State.COMPLETED
        coa.save(update_fields=["state", "updated_at"])
        from . import campaign as campaign_mod

        campaign_mod.on_coa_completed(coa)
        _enqueue_outcome(coa.pk)
    elif instance.status == Task.Status.CANCELLED and not active.exists():
        coa.state = CourseOfAction.State.ACCEPTED
        coa.save(update_fields=["state", "updated_at"])


def _enqueue_outcome(coa_id: int) -> None:
    from django.db import transaction

    from .tasks import measure_outcome

    transaction.on_commit(lambda: measure_outcome.delay(coa_id))
