"""Keep a finding consistent with the task created from it (design doc 12 §7.2).

When a readiness-generated task is marked ``done`` on the normal task board, its
linked finding is moved ``open → acknowledged``. The next ``compute_readiness`` is
the arbiter: if the gap is truly gone the finding becomes ``resolved``; if it still
measures the gap it returns to ``open`` and is eligible for a fresh task.
"""
from __future__ import annotations


def acknowledge_finding_on_task_done(sender, instance, **kwargs) -> None:
    if instance.related_type != "readiness" or instance.status != instance.Status.DONE:
        return
    from .models import ReadinessFinding

    ReadinessFinding.objects.filter(
        task=instance, status=ReadinessFinding.Status.OPEN
    ).update(status=ReadinessFinding.Status.ACKNOWLEDGED)


def connect() -> None:
    from django.db.models.signals import post_save

    from apps.tasks.models import Task

    post_save.connect(
        acknowledge_finding_on_task_done, sender=Task, dispatch_uid="readiness_ack_finding"
    )
