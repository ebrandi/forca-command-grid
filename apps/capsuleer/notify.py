"""Capsuleer Path → pingboard emission chokepoint (doc 13).

The **single** module that pushes a message out of ``apps.capsuleer``. Every career notification is
catalogued in ``apps.pingboard.notifications.REGISTRY`` (the four ``capsuleer.*`` keys, registered
before any emit path — an unregistered key would broadcast a personal milestone corp-wide) and
emitted here through ``apps.pingboard.services.emit_broadcast``.

Two-switch disarm (doc 13 §3): all four events ship inert. The chokepoint checks, in order, the
feature flag → the per-event arm in the capsuleer config (``capsuleer.notifications.enabled``,
default all false) → pingboard's own governance (``is_enabled``) → then builds a payload from the
per-event allowlist (doc 13 §7: titles at most; never motivation, budget, paused_reason, evidence,
profile fields, suggestion content, or another pilot's identity) and emits. Audience is always the
single-recipient ``{"kind": "user", "id": owner}`` form — the chokepoint never widens it. Every emit
is idempotency-keyed (doc 13 §6) and fail-soft: an emission failure never raises into the credit,
completion, review or generation paths.

Deep links are resolved with ``reverse()`` at emit time (the capsuleer namespace is mounted); a
resolution failure degrades to a bare path rather than breaking the emit.
"""
from __future__ import annotations

import logging

from apps.pingboard.models import AlertCategory

log = logging.getLogger("forca.capsuleer")

CATEGORY = AlertCategory.CAPSULEER

# Registry keys (doc 13 §2). The single source the registration test iterates — a fifth emitter key
# added without a REGISTRY entry fails ``test_emit_before_registration_impossible``.
MILESTONE_REACHED = "capsuleer.milestone_reached"
GOAL_COMPLETED = "capsuleer.goal_completed"
REVIEW_DUE = "capsuleer.review_due"
SUGGESTION = "capsuleer.suggestion"
EVENT_KEYS = (MILESTONE_REACHED, GOAL_COMPLETED, REVIEW_DUE, SUGGESTION)


def _reverse(name, *args, fallback):
    from django.urls import NoReverseMatch, reverse

    try:
        return reverse(name, args=list(args))
    except NoReverseMatch:
        return fallback


def _goal_path(goal_pk) -> str:
    return _reverse("capsuleer:goal_detail", goal_pk, fallback=f"/capsuleer/goals/{goal_pk}/")


def _review_path(goal_pk) -> str:
    return _reverse("capsuleer:goal_review", goal_pk, fallback=f"/capsuleer/goals/{goal_pk}/review/")


def _home_path() -> str:
    return _reverse("capsuleer:home", fallback="/capsuleer/")


def _abs(path: str) -> str:
    """Absolute deep link when a canonical base URL is configured, else the bare path."""
    try:
        from apps.pingboard import config as pb_config

        base = (pb_config.get("general") or {}).get("site_url") or ""
    except Exception:  # noqa: BLE001 — a link is best-effort, never a hard dependency
        base = ""
    return f"{base.rstrip('/')}{path}" if base else path


def _armed(key: str) -> bool:
    """The capsuleer-side per-event arm (default False — the disarm), keyed by event suffix."""
    from . import config

    suffix = key.split(".", 1)[1]
    return bool(config.get("notifications").get("enabled", {}).get(suffix, False))


def _emit(key, *, owner_id, title, body, source_object_id, idempotency_key, priority="low",
          template=None, context=None):
    """The one place a capsuleer alert is created (doc 13 §1, §3). Fail-soft; per-user DM only.

    ``template`` is an ``apps.pingboard.messages.SCAFFOLDS`` key and ``context`` its raw
    interpolation values (plain scalars) — together they let the alert re-render in the owner's
    own language. ``title``/``body`` remain the frozen English audit columns.
    """
    try:
        if not owner_id:
            return None
        from core.features import feature_enabled

        if not feature_enabled("capsuleer"):
            return None
        if not _armed(key):
            return None
        from apps.pingboard.notifications import is_enabled

        if not is_enabled(key):
            return None
        from apps.pingboard import services as pingboard

        alert = pingboard.emit_broadcast(
            category=CATEGORY, title=title, body=body,
            template=template, context=context,
            audience={"kind": "user", "id": owner_id},
            # In-app only, explicitly (doc 13 §4/§8): never fall through to every armed channel, so a
            # personal goal title can never be posted to a shared leadership channel (finding 14).
            channels=["in_app"],
            source_service="capsuleer", source_object_id=source_object_id,
            idempotency_key=idempotency_key, priority=priority,
        )
        # Log restraint (doc 13 §1): event key + object pk + emitted flag, never titles/values.
        log.info("capsuleer.notify key=%s obj=%s emitted=%s", key, source_object_id,
                 alert is not None)
        return alert
    except Exception:  # noqa: BLE001 — a notification must never break the business action
        log.exception("capsuleer.notify failed key=%s obj=%s", key, source_object_id)
        return None


def milestone_reached(goal, milestone):
    """DM the goal owner that a milestone was credited (doc 13 §7: goal + milestone titles only)."""
    body = (
        f"Milestone «{milestone.title}» on your goal «{goal.title}» is done. "
        f"{_abs(_goal_path(goal.pk))}"
    )
    return _emit(
        MILESTONE_REACHED, owner_id=goal.user_id,
        title="Milestone reached: «{milestone_title}»", body=body,
        template="capsuleer.milestone_reached",
        context={"milestone_title": milestone.title, "goal_title": goal.title,
                 "link": _abs(_goal_path(goal.pk))},
        source_object_id=str(milestone.pk),
        idempotency_key=f"capsuleer:milestone_reached:{milestone.pk}",
    )


def goal_completed(goal):
    """DM the goal owner that they completed a goal (doc 13 §7: goal title only)."""
    body = f"You completed your career goal «{goal.title}». {_abs(_goal_path(goal.pk))}"
    return _emit(
        GOAL_COMPLETED, owner_id=goal.user_id,
        title="Goal completed: «{goal_title}»", body=body,
        template="capsuleer.goal_completed",
        context={"goal_title": goal.title, "link": _abs(_goal_path(goal.pk))},
        source_object_id=str(goal.pk),
        idempotency_key=f"capsuleer:goal_completed:{goal.pk}",
    )


def review_due(goal, *, bucket):
    """DM the owner a gentle review nudge (doc 13 §7: goal title + review date). ``bucket`` is the
    ``YYYY-MM`` month that caps the nudge to one per goal per month."""
    # The review month is in the body (a permitted "review date", doc 13 §7) so a genuine
    # next-month nudge reads as new rather than being swallowed by pingboard's duplicate window.
    body = (
        f"It's been a while since you looked at «{goal.title}» ({bucket}). A two-minute review "
        f"keeps the plan honest — nothing changes if you don't. {_abs(_review_path(goal.pk))}"
    )
    return _emit(
        REVIEW_DUE, owner_id=goal.user_id,
        title="Review nudge: «{goal_title}»", body=body,
        template="capsuleer.review_due",
        context={"goal_title": goal.title, "review_month": bucket,
                 "link": _abs(_review_path(goal.pk))},
        source_object_id=str(goal.pk),
        idempotency_key=f"capsuleer:review_due:{goal.pk}:{bucket}",
    )


def suggestion_batch(user_id, count, *, day):
    """DM the pilot that new suggestions are waiting (doc 13 §7: count only — no titles/reasons/
    kinds). ``day`` is the ``YYYY-MM-DD`` batch key: one per user per generation run."""
    plural = "suggestion" if count == 1 else "suggestions"
    body = (
        f"You have {count} new Capsuleer Path {plural} waiting. Open your path to see them. "
        f"{_abs(_home_path())}"
    )
    return _emit(
        SUGGESTION, owner_id=user_id,
        title="New Capsuleer Path suggestions", body=body,
        # One scaffold key per English plural form: a gettext msgid is a whole sentence, so the
        # singular/plural split lives in the key, never in a (never-translated) slot value.
        template="capsuleer.suggestion.one" if count == 1 else "capsuleer.suggestion.many",
        context={"count": count, "link": _abs(_home_path())},
        source_object_id=str(user_id),
        idempotency_key=f"capsuleer:suggestion:{user_id}:{day}",
    )
