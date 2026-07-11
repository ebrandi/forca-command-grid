"""Capsuleer Path notifications (doc 13) — registration, disarmed defaults, allowlist, idempotency.

Pingboard is exercised for real (in-app leg); no provider mocks. Emits go through the ``notify.py``
chokepoint, which is disarmed by default and gated feature → config-arm → pingboard governance.
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal

import pytest

from apps.capsuleer import config, notify
from apps.capsuleer.models import GoalStatus, GoalType, MilestoneKind, Verification, Visibility
from apps.pingboard.models import Alert
from apps.pingboard.notifications import event

from ._capsuleer_utils import _character, _goal, _member, _milestone

pytestmark = pytest.mark.django_db


@contextmanager
def _armed(*events):
    """Arm the given capsuleer events on the emitter side for the duration of the block."""
    config.set("notifications", {"enabled": {e: True for e in events}})
    try:
        yield
    finally:
        config.reset("notifications")


def _capsuleer_alerts():
    return Alert.objects.filter(source_service="capsuleer")


def _pilot(django_user_model, cid=9501):
    user = _member(django_user_model, str(cid))
    return user, _character(user, cid, "Notify Pilot")


# --- registration (the corp-broadcast hazard guard) -------------------------
def test_all_event_keys_registered():
    for key in notify.EVENT_KEYS:
        assert event(key) is not None, key


def test_emit_before_registration_impossible():
    # The emitter's key tuple must be a subset of the registered catalogue — a fifth emitter key
    # without a REGISTRY entry fails here.
    assert set(notify.EVENT_KEYS) == {
        "capsuleer.milestone_reached", "capsuleer.goal_completed",
        "capsuleer.review_due", "capsuleer.suggestion",
    }
    for key in notify.EVENT_KEYS:
        assert event(key) is not None


# --- disarmed by default -----------------------------------------------------
def test_disarmed_by_default_no_alert_rows(django_user_model):
    user, char = _pilot(django_user_model)
    goal = _goal(user, character=char)
    ms = _milestone(goal)
    # Shipped config: every event false.
    notify.milestone_reached(goal, ms)
    notify.goal_completed(goal)
    notify.review_due(goal, bucket="2026-07")
    notify.suggestion_batch(user.pk, 3, day="2026-07-11")
    assert _capsuleer_alerts().count() == 0


# --- armed emit --------------------------------------------------------------
def test_armed_milestone_emits_user_dm(django_user_model):
    user, char = _pilot(django_user_model)
    goal = _goal(user, character=char, title="Fly logi")
    ms = _milestone(goal, title="Train remote reps")
    with _armed("milestone_reached"):
        alert = notify.milestone_reached(goal, ms)
    assert alert is not None
    assert alert.audience == {"kind": "user", "id": user.pk}
    assert alert.category == "capsuleer"
    assert alert.idempotency_key == f"capsuleer:milestone_reached:{ms.pk}"


def test_idempotency_key_dedupes(django_user_model):
    user, char = _pilot(django_user_model)
    goal = _goal(user, character=char)
    ms = _milestone(goal)
    with _armed("milestone_reached"):
        notify.milestone_reached(goal, ms)
        notify.milestone_reached(goal, ms)  # simulated hook + sweep race
    assert _capsuleer_alerts().filter(
        idempotency_key=f"capsuleer:milestone_reached:{ms.pk}"
    ).count() == 1


def test_goal_completed_once(django_user_model):
    user, char = _pilot(django_user_model)
    goal = _goal(user, character=char)
    with _armed("goal_completed"):
        notify.goal_completed(goal)
        notify.goal_completed(goal)  # reopen + re-complete does not re-celebrate
    assert _capsuleer_alerts().filter(idempotency_key=f"capsuleer:goal_completed:{goal.pk}").count() == 1


def test_review_due_monthly_bucket(django_user_model):
    user, char = _pilot(django_user_model)
    goal = _goal(user, character=char)
    with _armed("review_due"):
        notify.review_due(goal, bucket="2026-07")
        notify.review_due(goal, bucket="2026-07")   # same month → one
        notify.review_due(goal, bucket="2026-08")   # next month → a second
    keys = set(_capsuleer_alerts().values_list("idempotency_key", flat=True))
    assert f"capsuleer:review_due:{goal.pk}:2026-07" in keys
    assert f"capsuleer:review_due:{goal.pk}:2026-08" in keys


def test_suggestion_batch_single_dm_count_only(django_user_model):
    user, _ = _pilot(django_user_model)
    with _armed("suggestion"):
        alert = notify.suggestion_batch(user.pk, 4, day="2026-07-11")
        notify.suggestion_batch(user.pk, 7, day="2026-07-11")  # same day → still one
    assert _capsuleer_alerts().filter(
        idempotency_key=f"capsuleer:suggestion:{user.pk}:2026-07-11"
    ).count() == 1
    assert "4" in alert.body


# --- payload allowlist (doc 13 §7) ------------------------------------------
def test_payload_allowlist_never_leaks_forbidden_fields(django_user_model):
    user, char = _pilot(django_user_model)
    goal = _goal(
        user, character=char, title="Fly logi", visibility=Visibility.OFFICERS,
        motivation="SENTINEL_MOTIVE", budget_isk=Decimal("424242"),
        paused_reason="SENTINEL_PAUSE",
    )
    ms = _milestone(goal, title="Train reps", evidence_note="SENTINEL_EVIDENCE")
    with _armed("milestone_reached", "goal_completed"):
        a1 = notify.milestone_reached(goal, ms)
        a2 = notify.goal_completed(goal)
    for alert in (a1, a2):
        blob = f"{alert.title} {alert.body}"
        assert "Fly logi" in blob  # titles are permitted (owner-authored, owner-only)
        for forbidden in ("SENTINEL_MOTIVE", "SENTINEL_PAUSE", "SENTINEL_EVIDENCE", "424242"):
            assert forbidden not in blob


def test_owner_only_recipient_even_when_shared(django_user_model):
    """A goal shared at officers visibility still notifies the owner only — the chokepoint never
    widens the single-user audience."""
    user, char = _pilot(django_user_model)
    goal = _goal(user, character=char, visibility=Visibility.OFFICERS)
    ms = _milestone(goal)
    with _armed("milestone_reached"):
        alert = notify.milestone_reached(goal, ms)
    assert alert.audience == {"kind": "user", "id": user.pk}


# --- kill switches -----------------------------------------------------------
def test_feature_disable_silences_all(django_user_model):
    from core import features

    user, char = _pilot(django_user_model)
    goal = _goal(user, character=char)
    ms = _milestone(goal)
    features.set_disabled(["capsuleer"])
    try:
        with _armed("milestone_reached"):
            assert notify.milestone_reached(goal, ms) is None
    finally:
        features.set_disabled([])
    assert _capsuleer_alerts().count() == 0


def test_pingboard_governance_switch_wins(django_user_model):
    from apps.pingboard import config as pbconfig

    user, char = _pilot(django_user_model)
    goal = _goal(user, character=char)
    ms = _milestone(goal)
    pbconfig.set("notifications", {"events": {"capsuleer.milestone_reached": {"enabled": False}}})
    try:
        with _armed("milestone_reached"):
            assert notify.milestone_reached(goal, ms) is None
    finally:
        pbconfig.reset("notifications")


# --- service wiring emits on the credit path --------------------------------
def test_emit_on_owner_completion(django_user_model, django_capture_on_commit_callbacks):
    from apps.capsuleer import services

    user, char = _pilot(django_user_model)
    goal = services.create_goal(user, title="Fly logi", goal_type=GoalType.CUSTOM, character=char)
    goal = services.set_goal_status(goal, GoalStatus.ACTIVE, user)
    ms = _milestone(goal, kind=MilestoneKind.MANUAL, verification=Verification.SELF)
    with _armed("milestone_reached"):
        with django_capture_on_commit_callbacks(execute=True):
            services.complete_milestone(goal, ms, user)
    assert _capsuleer_alerts().filter(
        idempotency_key=f"capsuleer:milestone_reached:{ms.pk}"
    ).count() == 1
