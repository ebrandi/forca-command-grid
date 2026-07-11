"""Capsuleer Path HTTP surface (doc 10) — route happy paths, empty/degraded states, name resolution.

Route-level authorisation (the actor×route matrix) lives in ``test_capsuleer_security.py``; this file
proves each surface renders and each mutation lands for the owner, and that a seeded goal page shows
resolved names rather than raw ids.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.capsuleer import services
from apps.capsuleer.models import (
    CareerActionStep,
    CareerGoal,
    GoalStatus,
    GoalType,
    PathSuggestion,
    SuggestionKind,
    Verification,
    Visibility,
)
from apps.capsuleer.templates_builtin import sync_builtin_templates

from ._capsuleer_utils import _character, _member, _milestone, _officer

pytestmark = pytest.mark.django_db


def _login(client, user):
    client.force_login(user)
    return user


def _pilot(client, django_user_model, cid=41001):
    user = _member(django_user_model, str(cid))
    _character(user, cid, "View Pilot")
    _login(client, user)
    return user


# --- home --------------------------------------------------------------------
def test_home_empty_state(client, django_user_model):
    _pilot(client, django_user_model)
    resp = client.get(reverse("capsuleer:home"))
    assert resp.status_code == 200
    assert b"No goals yet" in resp.content


def test_home_with_goals(client, django_user_model):
    user = _pilot(client, django_user_model)
    services.create_goal(user, title="Fly logistics", goal_type=GoalType.CUSTOM)
    resp = client.get(reverse("capsuleer:home"))
    assert resp.status_code == 200
    assert b"Fly logistics" in resp.content


# --- wizard / browse / compare ----------------------------------------------
def test_wizard_get_and_skippable_post(client, django_user_model):
    user = _pilot(client, django_user_model)
    assert client.get(reverse("capsuleer:start")).status_code == 200
    # An entirely empty submit still works and lands on the catalogue.
    resp = client.post(reverse("capsuleer:start"), {})
    assert resp.status_code == 302 and "/capsuleer/paths/" in resp["Location"]
    user.refresh_from_db()
    assert user.career_profile.last_reviewed_at is not None


def test_paths_browse_and_filters(client, django_user_model):
    _pilot(client, django_user_model)
    sync_builtin_templates()
    resp = client.get(reverse("capsuleer:paths"))
    assert resp.status_code == 200 and b"Tackle Pilot" in resp.content
    # htmx partial
    resp = client.get(reverse("capsuleer:paths"), {"newbro": "on"}, HTTP_HX_REQUEST="true")
    assert resp.status_code == 200


def test_compare_page(client, django_user_model):
    _pilot(client, django_user_model)
    sync_builtin_templates()
    resp = client.get(reverse("capsuleer:compare"), {"keys": "tackle_pilot,explorer"})
    assert resp.status_code == 200 and b"Tackle Pilot" in resp.content


def test_path_detail_and_start(client, django_user_model):
    user = _pilot(client, django_user_model)
    sync_builtin_templates()
    resp = client.get(reverse("capsuleer:path_detail", args=["tackle_pilot"]))
    assert resp.status_code == 200 and b"Tackle Pilot" in resp.content
    resp = client.post(reverse("capsuleer:path_start", args=["tackle_pilot"]),
                       {"character_id": str(next(iter(user.characters.all())).character_id)})
    assert resp.status_code == 302
    assert CareerGoal.objects.filter(user=user, template_key="tackle_pilot").exists()


# --- goal create / detail / mutations ---------------------------------------
def test_goal_new_creates_custom_goal(client, django_user_model):
    user = _pilot(client, django_user_model)
    resp = client.post(reverse("capsuleer:goal_new"),
                       {"title": "My custom goal", "goal_type": "custom", "priority": "primary",
                        "pace": "inherit", "visibility": "private"})
    assert resp.status_code == 302
    assert CareerGoal.objects.filter(user=user, title="My custom goal").exists()


def test_goal_detail_resolves_names(client, django_user_model):
    user = _pilot(client, django_user_model)
    char = next(iter(user.characters.all()))
    goal = services.create_goal(user, title="Fly logi", goal_type=GoalType.CUSTOM, character=char)
    _milestone(goal, title="Train reps", verification=Verification.SELF)
    resp = client.get(reverse("capsuleer:goal_detail", args=[goal.pk]))
    assert resp.status_code == 200
    assert b"Fly logi" in resp.content
    assert b"View Pilot" in resp.content  # character name resolved, not a bare id


def test_goal_status_and_share_and_review(client, django_user_model):
    user = _pilot(client, django_user_model)
    goal = services.create_goal(user, title="g", goal_type=GoalType.CUSTOM)
    assert client.post(reverse("capsuleer:goal_status", args=[goal.pk]), {"to": "active"}).status_code == 302
    goal.refresh_from_db()
    assert goal.status == GoalStatus.ACTIVE
    assert client.post(reverse("capsuleer:goal_share", args=[goal.pk]),
                       {"visibility": "officers"}).status_code == 302
    goal.refresh_from_db()
    assert goal.visibility == Visibility.OFFICERS
    goal.review_due_at = goal.created_at
    goal.save(update_fields=["review_due_at"])
    assert client.post(reverse("capsuleer:goal_review", args=[goal.pk]), {}).status_code == 302
    goal.refresh_from_db()
    assert goal.review_due_at is None


def test_milestone_and_step_lifecycle(client, django_user_model):
    user = _pilot(client, django_user_model)
    goal = services.create_goal(user, title="g", goal_type=GoalType.CUSTOM, status=GoalStatus.ACTIVE)
    # add milestone
    assert client.post(reverse("capsuleer:milestone_add", args=[goal.pk]),
                       {"title": "Do a thing", "kind": "manual", "verification": "self",
                        "required": "on"}).status_code == 302
    ms = goal.milestones.get()
    assert client.post(reverse("capsuleer:milestone_status", args=[ms.pk]),
                       {"action": "done"}).status_code == 302
    ms.refresh_from_db()
    assert ms.status == "done"
    # steps
    assert client.post(reverse("capsuleer:step_add", args=[goal.pk]), {"title": "Buy a hull"}).status_code == 302
    step = goal.action_steps.get()
    assert client.post(reverse("capsuleer:step_status", args=[step.pk]), {"action": "done"}).status_code == 302


# --- profile -----------------------------------------------------------------
def test_profile_get_and_save(client, django_user_model):
    user = _pilot(client, django_user_model)
    assert client.get(reverse("capsuleer:profile")).status_code == 200
    resp = client.post(reverse("capsuleer:profile"),
                       {"pace": "relaxed", "corp_alignment": "personal_only",
                        "default_visibility": "mentor", "mute_stalled_goal": "on"})
    assert resp.status_code == 302
    p = user.career_profile
    p.refresh_from_db()
    assert p.pace == "relaxed" and p.corp_alignment == "personal_only"
    assert SuggestionKind.STALLED_GOAL in p.suggestion_muted_kinds


def test_profile_activity_lists_are_mutually_exclusive(client, django_user_model):
    # A value belongs to at most one list: avoided wins over preferred+curious, and
    # preferred wins over curious. "mining" is in all three, "industry" in pref+curious.
    user = _pilot(client, django_user_model)
    resp = client.post(reverse("capsuleer:profile"),
                       {"pace": "balanced", "corp_alignment": "balanced",
                        "default_visibility": "private",
                        "preferred_activities": "mining,industry",
                        "curious_activities": "mining,industry,exploration",
                        "avoided_activities": "mining"})
    assert resp.status_code == 302
    p = user.career_profile
    p.refresh_from_db()
    assert p.avoided_activities == ["mining"]
    assert "mining" not in p.preferred_activities and "mining" not in p.curious_activities
    assert p.preferred_activities == ["industry"]        # mining stripped (avoided wins)
    assert p.curious_activities == ["exploration"]        # mining->avoided, industry->preferred


# --- suggestions / quests ----------------------------------------------------
def test_suggestion_act(client, django_user_model):
    user = _pilot(client, django_user_model)
    row = PathSuggestion.objects.create(user=user, kind=SuggestionKind.STALLED_GOAL, title="t",
                                        reason="r", dedupe_key="u1:stalled_goal:goal:1:2026-07")
    assert client.post(reverse("capsuleer:suggestion_act", args=[row.pk]),
                       {"action": "dismiss"}).status_code == 302
    row.refresh_from_db()
    assert row.status == "dismissed"


def test_quest_action_done(client, django_user_model):
    user = _pilot(client, django_user_model)
    goal = services.create_goal(user, title="g", goal_type=GoalType.CUSTOM, status=GoalStatus.ACTIVE)
    step = CareerActionStep.objects.create(goal=goal, title="Next", source="pilot")
    assert client.post(reverse("capsuleer:quest_action", args=[step.pk]),
                       {"action": "done"}).status_code == 302
    step.refresh_from_db()
    assert step.status == "done"


# --- leadership + JSON pickers ----------------------------------------------
def test_leadership_officer_only(client, django_user_model):
    from apps.admin_audit.models import AuditLog

    member = _member(django_user_model, "lead_member")
    client.force_login(member)
    assert client.get(reverse("capsuleer:leadership")).status_code == 403
    officer = _officer(django_user_model, "lead_off")
    client.force_login(officer)
    assert client.get(reverse("capsuleer:leadership")).status_code == 200
    assert AuditLog.objects.filter(action="capsuleer.leadership.view").exists()


def test_json_pickers(client, django_user_model):
    _pilot(client, django_user_model)
    for name in ("capsuleer:types", "capsuleer:ships", "capsuleer:doctrines"):
        resp = client.get(reverse(name), {"q": "x"})
        assert resp.status_code == 200
        assert resp["Content-Type"].startswith("application/json")
