"""Signal wiring for Capsuleer Path (connected once at ``AppConfig.ready()``).

Closes the linked-task loop (doc 05 §5.2, ADR-0008): a ``tasks.Task`` soft-linked to a career action
step (``related_type="capsuleer_goal"``) that reaches ``done`` marks the step done (if still open) and
records a ``GoalActivity`` evidence row. It **never** completes the parent milestone or goal — task
completion only ever adds evidence (the campaigns rule; silent auto-completion breaks the manual/auto
conflict and forged-completion defences) — and it writes **no** contribution credit: the tasks app
already credited the ledger on DONE, and a second credit would double-count. A cancelled task leaves
the step untouched (the link renders inactive; Stage 4 surfaces it).
"""
from __future__ import annotations


def connect() -> None:
    from django.db.models.signals import post_save, pre_save

    from apps.tasks.models import Task

    pre_save.connect(_on_task_presave, sender=Task, dispatch_uid="capsuleer_task_presave")
    post_save.connect(_on_task_saved, sender=Task, dispatch_uid="capsuleer_task_rollup")


def _on_task_presave(sender, instance, **kwargs) -> None:
    """Snapshot the persisted status so the post-save handler can tell a real DONE transition from a
    no-op re-save (an admin title edit on an already-done task must not re-append evidence)."""
    from apps.tasks.models import Task

    from .services import TASK_RELATED_TYPE

    if instance.related_type != TASK_RELATED_TYPE:
        return
    if instance.pk is None:
        instance._capsuleer_prev_status = None
        return
    instance._capsuleer_prev_status = (
        Task.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    )


def _on_task_saved(sender, instance, created, **kwargs) -> None:
    from apps.tasks.models import Task

    from .services import TASK_RELATED_TYPE

    if instance.related_type != TASK_RELATED_TYPE:
        return
    if instance.status != Task.Status.DONE:  # only DONE adds evidence (cancel leaves the step)
        return
    prev_status = getattr(instance, "_capsuleer_prev_status", None)
    if not created and prev_status == instance.status:
        return  # a re-save of an already-done task is not a transition
    _credit_step(instance)


def _credit_step(task) -> None:
    from django.utils import timezone

    from . import services
    from .models import CareerActionStep, StepStatus

    # The step's ``task_id`` is the authoritative link (set when the task was created).
    step = CareerActionStep.objects.filter(task_id=task.pk).select_related("goal").first()
    if step is None:  # dangling link (step deleted) — inert by design
        return

    if step.status == StepStatus.OPEN:
        step.status = StepStatus.DONE
        step.completed_at = timezone.now()
        step.save(update_fields=["status", "completed_at", "updated_at"])
    # Evidence only — never the milestone/goal, never a second contribution credit.
    services.record_activity(step.goal, None, "step.task_done",
                             {"step_id": step.pk, "task_id": task.pk})
