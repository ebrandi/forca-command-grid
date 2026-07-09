"""Begin / end an impersonation: the session bookkeeping + audit writes.

Kept separate from the pure :mod:`policy` predicate so callers arm/disarm through one
audited seam. ``begin`` assumes the caller already verified ``policy.can_impersonate``.
"""
from __future__ import annotations

import time

from django.utils import timezone

from core.audit import audit_log, client_ip

from . import policy
from .models import ImpersonationSession


def _label(user) -> str:
    if user is None:
        return ""
    name = getattr(user, "display_name", "") or getattr(user, "get_username", lambda: "")()
    return (name or "")[:200]


def begin(request, target, *, reason: str = "") -> ImpersonationSession:
    """Record + arm an impersonation on ``request.session``. Caller MUST have already
    verified ``policy.can_impersonate(request.user, target)``."""
    actor = request.user
    ip = client_ip(request)
    record = ImpersonationSession.objects.create(
        actor=actor if getattr(actor, "pk", None) else None,
        target=target,
        actor_label=_label(actor),
        target_label=_label(target),
        reason=(reason or "")[:200],
        ip=ip or None,
    )
    request.session[policy.SESSION_TARGET_KEY] = target.pk
    request.session[policy.SESSION_ACTOR_KEY] = actor.pk
    request.session[policy.SESSION_RECORD_KEY] = record.pk
    request.session[policy.SESSION_STARTED_KEY] = int(time.time())
    request.session.modified = True
    audit_log(
        actor, "impersonation.start", target_type="user", target_id=str(target.pk),
        metadata={"session_id": record.pk, "target": record.target_label, "reason": record.reason},
        ip=ip,
    )
    return record


def end(request, *, reason: str, actor=None) -> None:
    """Close the active impersonation: stamp the record, audit, clear the session keys.

    Idempotent + defensive (safe when nothing is active). ``actor`` is the REAL director
    (``request.impersonator`` on a swapped request); we fall back to the session's recorded
    actor id purely for the audit metadata.
    """
    session = request.session
    record_id = session.get(policy.SESSION_RECORD_KEY)
    target_id = session.get(policy.SESSION_TARGET_KEY)
    actor_id = session.get(policy.SESSION_ACTOR_KEY)
    # Clear the keys FIRST so any failure below can't leave a wedged, still-swapping session.
    for key in (
        policy.SESSION_TARGET_KEY, policy.SESSION_ACTOR_KEY,
        policy.SESSION_RECORD_KEY, policy.SESSION_STARTED_KEY,
    ):
        session.pop(key, None)
    session.modified = True
    if record_id:
        record = ImpersonationSession.objects.filter(pk=record_id, ended_at__isnull=True).first()
        if record is not None:
            record.ended_at = timezone.now()
            record.end_reason = reason
            record.save(update_fields=["ended_at", "end_reason"])
    audit_log(
        actor if getattr(actor, "pk", None) else None,
        "impersonation.end", target_type="user", target_id=str(target_id or ""),
        metadata={"session_id": record_id, "reason": reason, "actor_id": actor_id},
        ip=client_ip(request),
    )
