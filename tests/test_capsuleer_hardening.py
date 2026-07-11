"""Capsuleer Path Stage-5 hardening regressions.

One focused test per confirmed adversarial finding that changed behaviour: impersonation budget
masking (1-5), self-endorsement forgery (6-7), silently-ignored visibility edit (8), corp-task
neutral title (9), leadership suppression uniformity + completed floor (10-11), GDPR authorship
severing (12), terminal-goal mutation guard (13), free-text length caps (17), NaN/Infinity budget
rejection (18), and goalless-widening avoided honouring (28).
"""
from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.capsuleer import config, services, suggest
from apps.capsuleer.models import (
    CareerActionStep,
    GoalActivity,
    GoalStatus,
    GoalType,
    StepSource,
    Verification,
    Visibility,
)

from ._capsuleer_utils import _character, _director, _goal, _member, _milestone, _officer, _profile

pytestmark = pytest.mark.django_db


def _owned(client, django_user_model, cid=51001, **goal_over):
    user = _member(django_user_model, str(cid))
    char = _character(user, cid, "Owner Pilot")
    user.main_character_id = cid
    user.save(update_fields=["main_character_id"])
    fields = {"character": char, "status": GoalStatus.ACTIVE, "budget_isk": 5_000_000,
              "motivation": "get out of this corp"}
    fields.update(goal_over)
    goal = _goal(user, **fields)
    return user, char, goal


# --- impersonation masking (findings 1-5) ------------------------------------
def test_impersonation_masks_goal_budget(client, django_user_model):
    owner, _char, goal = _owned(client, django_user_model)
    # The real owner sees their budget.
    client.force_login(owner)
    body = client.get(reverse("capsuleer:goal_detail", args=[goal.pk])).content
    assert b"Your budget" in body
    # A director view-as does NOT (N-class masked on the real actor, not request.user).
    director = _director(django_user_model, "imp_dir")
    director.main_character_id = 2001
    director.save(update_fields=["main_character_id"])
    client.force_login(director)
    assert client.post(f"/impersonation/start/{owner.id}/").status_code == 302
    resp = client.get(reverse("capsuleer:goal_detail", args=[goal.pk]))
    assert resp.status_code == 200
    assert b"Your budget" not in resp.content  # budget masked under view-as


def test_impersonation_edit_masks_budget_keeps_motivation(client, django_user_model):
    owner, _char, goal = _owned(client, django_user_model, cid=51002)
    director = _director(django_user_model, "imp_dir2")
    director.main_character_id = 2002
    director.save(update_fields=["main_character_id"])
    client.force_login(director)
    client.post(f"/impersonation/start/{owner.id}/")
    resp = client.get(reverse("capsuleer:goal_edit", args=[goal.pk]))
    assert resp.status_code == 200
    body = resp.content.decode()
    # Motivation is O-class — it DOES render under view-as (doc 09 §1.3).
    assert "get out of this corp" in body
    # Budget is N-class — its input is blank under view-as (the value never reaches the template).
    assert 'name="budget_isk" inputmode="decimal" value="5000000"' not in body


def test_impersonation_profile_masks_budget(client, django_user_model):
    owner = _member(django_user_model, "51003")
    _character(owner, 51003, "P")
    owner.main_character_id = 51003
    owner.save(update_fields=["main_character_id"])
    _profile(owner, monthly_budget_isk=9_000_000)
    director = _director(django_user_model, "imp_dir3")
    director.main_character_id = 2003
    director.save(update_fields=["main_character_id"])
    client.force_login(director)
    client.post(f"/impersonation/start/{owner.id}/")
    resp = client.get(reverse("capsuleer:profile"))
    assert resp.status_code == 200
    assert b"9000000" not in resp.content  # monthly budget masked under view-as


# --- self-endorsement forgery (findings 6-7) ---------------------------------
def test_officer_cannot_self_endorse(django_user_model):
    officer = _officer(django_user_model, "self_off")
    goal = _goal(officer, visibility=Visibility.OFFICERS, status=GoalStatus.ACTIVE)
    ms = _milestone(goal, verification=Verification.OFFICER)
    with pytest.raises(ValidationError):
        services.endorse_milestone(goal, ms, officer)


def test_mentorship_self_pairing_rejected(django_user_model):
    from apps.mentorship.models import MenteeProfile, MentorProfile
    from apps.mentorship.services import propose_pairing

    user = _member(django_user_model, "selfpair")
    mentor = MentorProfile.objects.create(user=user, status=MentorProfile.Status.ACTIVE)
    mentee = MenteeProfile.objects.create(user=user)
    assert propose_pairing(mentor, mentee, initiated_by="self") is None


# --- silently-ignored visibility edit (finding 8) ----------------------------
def test_visibility_edit_applies_and_audits(client, django_user_model):
    owner, _char, goal = _owned(client, django_user_model, cid=51004,
                                visibility=Visibility.OFFICERS)
    client.force_login(owner)
    resp = client.post(reverse("capsuleer:goal_edit", args=[goal.pk]), {
        "title": goal.title, "priority": "secondary", "pace": "inherit", "visibility": "private",
    })
    assert resp.status_code == 302
    goal.refresh_from_db()
    assert goal.visibility == Visibility.PRIVATE  # narrowing actually took effect
    assert GoalActivity.objects.filter(goal=goal, verb="visibility.changed").exists()


# --- corp-task neutral title (finding 9) -------------------------------------
def test_corp_task_neutral_title_strips_goal(django_user_model):
    from core.features import feature_enabled

    if not feature_enabled("tasks"):
        pytest.skip("tasks feature disabled")
    owner = _member(django_user_model, "corp_task_owner")
    goal = _goal(owner, title="Become a Black Ops pilot", visibility=Visibility.PRIVATE,
                 status=GoalStatus.ACTIVE)
    step = CareerActionStep.objects.create(
        goal=goal, title="Mentors available for «Become a Black Ops pilot»",
        source=StepSource.SUGGESTION, status="open",
    )
    task = services.make_corp_task_from_step(goal, step, owner)
    assert "Black Ops" not in task.title and "«" not in task.title


# --- leadership suppression uniformity + completed floor (findings 10-11) -----
def test_leadership_drops_below_floor_groups(django_user_model):
    config.set("leadership", {"min_group": 2})
    for i in range(2):
        _goal(_member(django_user_model, f"alpha{i}"), visibility=Visibility.OFFICERS,
              goal_type=GoalType.TEMPLATE, template_key="alpha", title="A")
    for i in range(2):
        _goal(_member(django_user_model, f"beta{i}"), visibility=Visibility.OFFICERS,
              goal_type=GoalType.TEMPLATE, template_key="beta", title="B")
    _goal(_member(django_user_model, "gamma0"), visibility=Visibility.OFFICERS,
          goal_type=GoalType.TEMPLATE, template_key="gamma", title="C")
    pipe = services.leadership_pipeline()
    labels = {r["label"] for r in pipe["by_template"]}
    assert "gamma" not in labels  # a below-floor group is dropped, not shown as "suppressed"
    assert {"alpha", "beta"} <= labels  # published groups survive


def test_leadership_completed_count_floored(django_user_model):
    config.set("leadership", {"min_group": 2})
    for i in range(3):
        _goal(_member(django_user_model, f"live{i}"), visibility=Visibility.OFFICERS)
    g = _goal(_member(django_user_model, "done0"), visibility=Visibility.OFFICERS,
              status=GoalStatus.COMPLETED)
    g.completed_at = timezone.now()
    g.save(update_fields=["completed_at"])
    pipe = services.leadership_pipeline()
    assert pipe["completed_90d"]["suppressed"] is True  # exact 1 is never published


# --- GDPR authorship severing (finding 12) -----------------------------------
def test_gdpr_anonymises_authored_activity(django_user_model):
    from apps.identity.services import delete_user_data

    mentor = _member(django_user_model, "gdpr_mentor")
    mentee = _member(django_user_model, "gdpr_mentee")
    goal = _goal(mentee, visibility=Visibility.MENTOR, status=GoalStatus.ACTIVE)
    note = services.record_activity(goal, mentor, services.V_MENTOR_NOTE, {"text": "keep it up"})
    delete_user_data(mentor)
    note.refresh_from_db()
    assert note.actor_id is None  # authorship severed
    assert GoalActivity.objects.filter(pk=note.pk).exists()  # note survives for the owner


# --- terminal-goal mutation guard (finding 13) -------------------------------
def test_terminal_goal_blocks_milestone_mutation(django_user_model):
    owner = _member(django_user_model, "term_owner")
    goal = _goal(owner, status=GoalStatus.COMPLETED)
    with pytest.raises(ValidationError):
        services.add_milestone(goal, owner, kind="manual", title="late add",
                               verification=Verification.SELF)


@pytest.mark.parametrize("terminal", [GoalStatus.COMPLETED, GoalStatus.ARCHIVED])
def test_terminal_goal_blocks_milestone_update_view(client, django_user_model, terminal):
    owner = _member(django_user_model, f"mu_{terminal}")
    _character(owner, 54000 + hash(terminal) % 900, "P")
    goal = _goal(owner, status=terminal)
    ms = _milestone(goal, title="frozen", verification=Verification.SELF)
    client.force_login(owner)
    resp = client.post(reverse("capsuleer:milestone_update", args=[ms.pk]),
                       {"title": "edited", "required": "on"})
    assert resp.status_code == 302
    ms.refresh_from_db()
    assert ms.title == "frozen"  # a frozen goal's milestone was not mutated


def test_terminal_goal_blocks_endorsement(django_user_model):
    owner = _member(django_user_model, "endo_owner")
    officer = _officer(django_user_model, "endo_off")
    goal = _goal(owner, visibility=Visibility.OFFICERS, status=GoalStatus.COMPLETED)
    ms = _milestone(goal, verification=Verification.OFFICER)
    with pytest.raises(ValidationError):
        services.endorse_milestone(goal, ms, officer)


# --- panel budget defence-in-depth (impersonation footgun) -------------------
def test_dashboard_panel_never_carries_budget(django_user_model):
    owner = _member(django_user_model, "panel_owner")
    _character(owner, 54500, "P")
    services.create_goal(owner, title="g", goal_type=GoalType.CUSTOM, status=GoalStatus.ACTIVE,
                         budget_isk=7_000_000)
    panel = services.dashboard_panel(owner)
    assert panel is not None and panel["goal"].budget_isk is None


# --- free-text length caps (finding 17) --------------------------------------
def test_motivation_length_capped(django_user_model):
    owner = _member(django_user_model, "cap_owner")
    goal = services.create_goal(owner, title="g", goal_type=GoalType.CUSTOM,
                                motivation="x" * 5000)
    assert len(goal.motivation) == 2000


# --- NaN/Infinity budget rejection (finding 18) ------------------------------
@pytest.mark.parametrize("idx,bad", list(enumerate(["NaN", "Infinity", "-Infinity", "1E+100000"])))
def test_nan_infinity_budget_rejected(client, django_user_model, idx, bad):
    user = _member(django_user_model, f"nan_{idx}")
    _character(user, 52000 + idx, "N")
    client.force_login(user)
    resp = client.post(reverse("capsuleer:goal_new"), {
        "title": "budget test", "goal_type": "custom", "priority": "secondary",
        "pace": "inherit", "visibility": "private", "budget_isk": bad,
    })
    assert resp.status_code == 302  # never a 500
    goal = services.CareerGoal.objects.filter(user=user, title="budget test").first()
    assert goal is not None and goal.budget_isk is None  # poisoned value never persisted


# --- goalless-widening avoided honouring (finding 28) ------------------------
def test_goalless_event_match_honours_avoided(django_user_model):
    from datetime import timedelta

    from apps.operations.models import Operation

    user = _member(django_user_model, "avoid_ev")
    _character(user, 53001, "A")
    _profile(user, corp_alignment="corp_forward", preferred_activities=["mining"],
             avoided_activities=["mining"])
    Operation.objects.create(name="Moon dig", type=Operation.Type.MINING,
                             target_at=timezone.now() + timedelta(days=1),
                             status=Operation.Status.PLANNED)
    ctx = suggest._build_context(user, timezone.now())
    assert [d for d in suggest.gen_event_match(ctx) if d.goal_id is None] == []


def test_goalless_near_qual_honours_avoided(django_user_model, monkeypatch):
    from apps.doctrines.models import Doctrine, DoctrineCategory

    user = _member(django_user_model, "avoid_nq")
    char = _character(user, 53002, "A")
    user.main_character_id = 53002
    user.save(update_fields=["main_character_id"])
    _profile(user, corp_alignment="corp_forward", preferred_activities=["industry"],
             avoided_activities=["combat_line", "combat_support", "tackle_scout",
                                 "fleet_command", "black_ops", "capitals"])
    cat = DoctrineCategory.objects.get_or_create(key="dps", label="DPS")[0]
    doc = Doctrine.objects.create(name="Tackle Wing", category=cat,
                                  status=Doctrine.Status.ACTIVE, priority=9)
    char.skill_snapshots.create(is_latest=True, skills={})
    # Bypass the closeness machinery — the point under test is the avoided/preferred gate only.
    monkeypatch.setattr(suggest, "_near_qual_for", lambda *a, **k: (doc, 1, 3))
    monkeypatch.setattr(suggest, "_main_character", lambda ctx: char)
    ctx = suggest._build_context(user, timezone.now())
    assert [d for d in suggest.gen_near_qualification(ctx) if d.corp_driven] == []


def test_goalless_near_qual_fires_for_combat_preferrer(django_user_model, monkeypatch):
    from apps.doctrines.models import Doctrine, DoctrineCategory

    user = _member(django_user_model, "combat_nq")
    char = _character(user, 53003, "A")
    user.main_character_id = 53003
    user.save(update_fields=["main_character_id"])
    _profile(user, corp_alignment="corp_forward", preferred_activities=["combat_line"])
    cat = DoctrineCategory.objects.get_or_create(key="dps", label="DPS")[0]
    doc = Doctrine.objects.create(name="Tackle Wing", category=cat,
                                  status=Doctrine.Status.ACTIVE, priority=9)
    char.skill_snapshots.create(is_latest=True, skills={})
    monkeypatch.setattr(suggest, "_near_qual_for", lambda *a, **k: (doc, 1, 3))
    monkeypatch.setattr(suggest, "_main_character", lambda ctx: char)
    ctx = suggest._build_context(user, timezone.now())
    assert any(d.corp_driven for d in suggest.gen_near_qualification(ctx))
