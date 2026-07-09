"""Background tasks for the skills app."""
from __future__ import annotations

import logging

from celery import shared_task

log = logging.getLogger("forca.skills")


@shared_task(name="skills.notify_idle_queues")
def notify_idle_queues() -> int:
    """DM opted-in pilots one reminder when a character's skill queue runs dry.

    No-op unless pilots have opted in and the ``skills.idle_queue`` event is enabled."""
    from .idle_notify import notify_idle_queues as _notify

    sent = _notify()
    if sent:
        log.info("sent %s idle-queue nudge(s)", sent)
    return sent
