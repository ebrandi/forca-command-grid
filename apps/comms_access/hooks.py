"""Targeted-reconcile hooks — the fast revoke path.

FORCA has no single access-change event bus; access flows through a few explicit
reconcilers (``sso.services.sync_roles_for_user`` on corp-leave / director change,
``sso.views.disconnect_view`` on token revoke). We enqueue a comms reconcile from those
chokepoints so a departure propagates to Discord/Slack/Mumble in near-real-time instead of
waiting for the periodic sweep.

``enqueue_user_reconcile`` is guarded by ``config.feature_active()`` (the ops env kill switch
is checked before any config read, so a hard-disabled deployment pays only an attribute
lookup on the login hot path) and swallows every error — a comms hiccup must never break
login or disconnect.
"""
from __future__ import annotations

import logging

log = logging.getLogger("forca.comms_access")


def enqueue_user_reconcile(user, *, source_ref: str = "") -> None:
    """Best-effort: queue a targeted reconcile for ``user``. No-op when the feature is off."""
    from . import config

    if not config.feature_active():
        return
    user_id = getattr(user, "pk", None)
    if not user_id:
        return
    try:
        from .tasks import reconcile_user_task

        reconcile_user_task.delay(user_id, source_ref=source_ref)
    except Exception:  # noqa: BLE001 - never break the caller's flow (login / disconnect)
        log.warning("comms_access: failed to enqueue reconcile for user %s", user_id)
