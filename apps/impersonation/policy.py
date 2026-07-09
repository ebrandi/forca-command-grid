"""Impersonation policy: who may view-as whom, session keys, and the duration cap.

Pure predicates + constants — no side effects — so both the middleware (per-request
re-validation) and the views (entry gate) enforce exactly the same rule.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings

from core import rbac

# Django-session keys carrying an active impersonation (namespaced so nothing collides).
SESSION_TARGET_KEY = "_impersonate_target_id"
SESSION_ACTOR_KEY = "_impersonate_actor_id"
SESSION_RECORD_KEY = "_impersonate_session_id"
SESSION_STARTED_KEY = "_impersonate_started_at"  # epoch seconds

DEFAULT_MAX_MINUTES = 30


def max_duration() -> timedelta:
    """How long an impersonation lasts before the middleware auto-exits it. Env/settings
    overridable (``IMPERSONATION_MAX_MINUTES``); clamped to a sane 1..1440 range."""
    minutes = getattr(settings, "IMPERSONATION_MAX_MINUTES", DEFAULT_MAX_MINUTES)
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        minutes = DEFAULT_MAX_MINUTES
    return timedelta(minutes=min(1440, max(1, minutes)))


def can_impersonate(actor, target) -> bool:
    """True if ``actor`` may view the site as ``target``.

    The single security rule, re-checked on every request by the middleware so a
    mid-session role change ends the session immediately:

    * both accounts active; never yourself;
    * ``actor`` must be a director or admin (rank >= director);
    * ``target`` must never be a platform superuser (that would be an escalation);
    * ``target`` must be STRICTLY below ``actor``'s effective rank — a director can view
      members / officers / lateral-role holders; only an admin (rank 40) can view a
      director (rank 30). No one can view a peer or a higher rank.
    """
    if actor is None or target is None:
        return False
    if not getattr(actor, "is_authenticated", False):
        return False
    if not getattr(actor, "is_active", True) or not getattr(target, "is_active", True):
        return False
    if getattr(actor, "pk", None) is None or actor.pk == getattr(target, "pk", None):
        return False
    if not rbac.has_role(actor, rbac.ROLE_DIRECTOR):
        return False
    if getattr(target, "is_superuser", False):
        return False
    return rbac.effective_rank(target) < rbac.effective_rank(actor)
