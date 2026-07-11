"""Quest-queue merge with the capsuleer ``career`` param (doc 08 §10, doc 10 §5.12).

The career row appears, byte-identical behaviour with ``career=()`` (the pinned merge tests are
untouched), and the collision-yield rule that drops the career row when a surviving item shares its
subject hull/doctrine.
"""
from __future__ import annotations

import pytest

from apps.pilots.briefing import _career_subject_collides, unified_quest_queue

from ._capsuleer_utils import _character, _member, _ship_type

pytestmark = pytest.mark.django_db


def _career_row(**over):
    row = {
        "engine": "capsuleer", "id": 1, "category_key": "capsuleer", "category_label": "Capsuleer Path",
        "icon": "#i-route", "corp_order": False, "title": "Fit your first Osprey",
        "detail": "next step", "points": 5, "action_url": "/capsuleer/goals/1/", "action_available": True,
        "form_url_name": "capsuleer:quest_action", "is_new": False, "rank": 505,
        "subject_doctrine_id": None, "subject_ship_type_id": None, "goal_id": 1,
    }
    row.update(over)
    return row


def test_career_empty_is_byte_identical(db):
    assert unified_quest_queue([], []) == unified_quest_queue([], [], career=())


def test_career_row_appears(db):
    out = unified_quest_queue([], [], career=[_career_row()])
    assert len(out) == 1 and out[0]["engine"] == "capsuleer"


def test_collision_yield_on_ship_subject(db):
    _ship_type(11985, "Osprey")
    # A surviving item already asks for the Osprey → the career row is suppressed.
    assert _career_subject_collides(_career_row(subject_ship_type_id=11985), "get your osprey now") is True
    # No surviving mention → the career row stays.
    assert _career_subject_collides(_career_row(subject_ship_type_id=11985), "train gunnery") is False


def test_career_quests_row_shape(client, django_user_model):
    from apps.capsuleer import services
    from apps.capsuleer.briefing import career_quests
    from apps.capsuleer.models import CareerActionStep, GoalStatus, GoalType

    user = _member(django_user_model, "cq")
    _character(user, 43001, "CQ Pilot")
    goal = services.create_goal(user, title="Fly logi", goal_type=GoalType.CUSTOM,
                                status=GoalStatus.ACTIVE)
    CareerActionStep.objects.create(goal=goal, title="Read the primer", source="pilot")
    rows = career_quests(user)
    assert len(rows) == 1
    r = rows[0]
    assert r["form_url_name"] == "capsuleer:quest_action" and r["points"] == 5
    assert r["action_url"] == f"/capsuleer/goals/{goal.pk}/"
