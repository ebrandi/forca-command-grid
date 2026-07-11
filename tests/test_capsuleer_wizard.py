"""Capsuleer Path wizard + goal-creation entry modes (doc 05 §2, doc 10 §5.2, §5.6).

Every wizard question is skippable; each goal_new entry mode (custom/doctrine/ship/activity) and
path_start create the right goal shape.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.capsuleer.models import CareerGoal, GoalType
from apps.capsuleer.templates_builtin import sync_builtin_templates
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit

from ._capsuleer_utils import _character, _member

pytestmark = pytest.mark.django_db


def _pilot(client, django_user_model, cid=44001):
    user = _member(django_user_model, str(cid))
    _character(user, cid, "Wizard Pilot")
    client.force_login(user)
    return user


def test_wizard_writes_only_answered_fields(client, django_user_model):
    user = _pilot(client, django_user_model)
    client.post(reverse("capsuleer:start"),
                {"pace": "accelerated", "preferred_activities": "mining,industry"})
    p = user.career_profile
    p.refresh_from_db()
    assert p.pace == "accelerated"
    assert set(p.preferred_activities) == {"mining", "industry"}
    assert p.weekly_hours is None  # unanswered → untouched


def test_wizard_avoided_wins_over_preferred(client, django_user_model):
    user = _pilot(client, django_user_model)
    client.post(reverse("capsuleer:start"),
                {"preferred_activities": "mining,combat_line", "avoided_activities": "combat_line"})
    p = user.career_profile
    p.refresh_from_db()
    assert "combat_line" not in p.preferred_activities
    assert "combat_line" in p.avoided_activities


def test_goal_new_doctrine_mode(client, django_user_model):
    user = _pilot(client, django_user_model)
    cat, _ = DoctrineCategory.objects.get_or_create(key="logi", label="Logi")
    doctrine = Doctrine.objects.create(name="Armor Logi", category=cat, priority=80)
    DoctrineFit.objects.create(doctrine=doctrine, name="Guardian", ship_type_id=11985)
    client.post(reverse("capsuleer:goal_new"),
                {"title": "Fly the doctrine", "goal_type": "doctrine", "doctrine_id": str(doctrine.id),
                 "priority": "primary", "pace": "inherit", "visibility": "private"})
    goal = CareerGoal.objects.get(user=user, title="Fly the doctrine")
    assert goal.goal_type == GoalType.DOCTRINE and goal.doctrine_id == doctrine.id


def test_goal_new_ship_mode(client, django_user_model):
    user = _pilot(client, django_user_model)
    client.post(reverse("capsuleer:goal_new"),
                {"title": "Fly an Osprey", "goal_type": "ship", "ship_type_id": "11985",
                 "priority": "secondary", "pace": "inherit", "visibility": "private"})
    goal = CareerGoal.objects.get(user=user, title="Fly an Osprey")
    assert goal.goal_type == GoalType.SHIP and goal.ship_type_id == 11985


def test_goal_new_activity_mode(client, django_user_model):
    user = _pilot(client, django_user_model)
    client.post(reverse("capsuleer:goal_new"),
                {"title": "Mine more", "goal_type": "activity", "activity": "mining",
                 "priority": "someday", "pace": "inherit", "visibility": "private"})
    goal = CareerGoal.objects.get(user=user, title="Mine more")
    assert goal.goal_type == GoalType.ACTIVITY and goal.activity == "mining"


def test_path_start_creates_template_goal_with_milestones(client, django_user_model):
    user = _pilot(client, django_user_model)
    sync_builtin_templates()
    char = next(iter(user.characters.all()))
    # Resolve tackle ship names so ship_owned milestones instantiate.
    from ._capsuleer_utils import _ship_type
    for tid, name in [(11400, "Atron"), (11401, "Slasher"), (11402, "Executioner"), (11403, "Merlin")]:
        _ship_type(tid, name)
    resp = client.post(reverse("capsuleer:path_start", args=["tackle_pilot"]),
                       {"character_id": str(char.character_id)})
    assert resp.status_code == 302
    goal = CareerGoal.objects.get(user=user, template_key="tackle_pilot")
    assert goal.goal_type == GoalType.TEMPLATE and goal.milestones.exists()
