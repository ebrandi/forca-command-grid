"""Capsuleer Path object-security tests (doc 09 Part 9).

Two layers. The *service-layer* chokepoint — ``visible_goals`` / ``can_view_goal`` across the
visibility tier matrix, field-level budget masking, and the rule that ``motivation`` is never
exposed by any shared-tier helper — walked directly against the services. And the *route-level*
matrix (doc 09 §9): the 404-not-403 policy on object routes, budget/motivation absent from every
non-owner response body, endorse/note tier rules, non-owner mutations 404, and the officer-only
audited leadership page.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib.auth.models import AnonymousUser

from apps.capsuleer import services
from apps.capsuleer.models import Visibility
from apps.mentorship.models import MentorshipPairing

from ._capsuleer_utils import _director, _goal, _member, _officer, _pair

pytestmark = pytest.mark.django_db

VIS = Visibility


@pytest.fixture
def matrix(django_user_model):
    """One goal per visibility tier owned by ``owner``, plus the actor set of doc 09 §9."""
    owner = _member(django_user_model, "owner")
    member = _member(django_user_model, "member")
    officer = _officer(django_user_model, "officer")
    director = _director(django_user_model, "director")
    mentor_ok = _member(django_user_model, "mentor_ok")
    mentor_gone = _member(django_user_model, "mentor_gone")
    other = _member(django_user_model, "other")

    _pair(mentor_ok, owner, status=MentorshipPairing.Status.ACTIVE)
    _pair(mentor_gone, owner, status=MentorshipPairing.Status.COMPLETED)

    goals = {
        VIS.PRIVATE: _goal(owner, visibility=VIS.PRIVATE, title="private plan"),
        VIS.MENTOR: _goal(owner, visibility=VIS.MENTOR, title="mentor plan"),
        VIS.OFFICERS: _goal(owner, visibility=VIS.OFFICERS, title="officers plan"),
        VIS.AGGREGATE_ONLY: _goal(owner, visibility=VIS.AGGREGATE_ONLY, title="aggregate plan"),
    }
    return {
        "owner": owner, "member": member, "officer": officer, "director": director,
        "mentor_ok": mentor_ok, "mentor_gone": mentor_gone, "other": other, "goals": goals,
    }


def _can_view(user, goal):
    return services.can_view_goal(user, goal)


# --- owner: sees every tier ---------------------------------------------------
def test_owner_sees_all_tiers(matrix):
    owner, goals = matrix["owner"], matrix["goals"]
    for goal in goals.values():
        assert _can_view(owner, goal)
    assert set(services.visible_goals(owner).values_list("pk", flat=True)) == {
        g.pk for g in goals.values()
    }


# --- plain member / unrelated user: sees nothing of the owner's --------------
@pytest.mark.parametrize("actor_key", ["member", "other"])
def test_unrelated_member_sees_no_goal(matrix, actor_key):
    actor = matrix[actor_key]
    for goal in matrix["goals"].values():
        assert not _can_view(actor, goal)
    assert services.visible_goals(actor).count() == 0


# --- officer: officers-tier only ---------------------------------------------
def test_officer_sees_officers_tier_only(matrix):
    officer, goals = matrix["officer"], matrix["goals"]
    assert _can_view(officer, goals[VIS.OFFICERS])
    for tier in (VIS.PRIVATE, VIS.MENTOR, VIS.AGGREGATE_ONLY):
        assert not _can_view(officer, goals[tier])
    assert set(services.visible_goals(officer).values_list("pk", flat=True)) == {
        goals[VIS.OFFICERS].pk
    }


def test_director_inherits_officer_read(matrix):
    director, goals = matrix["director"], matrix["goals"]
    assert _can_view(director, goals[VIS.OFFICERS])
    assert not _can_view(director, goals[VIS.PRIVATE])
    assert not _can_view(director, goals[VIS.MENTOR])


# --- mentor with an active pairing: mentor-tier only -------------------------
def test_active_mentor_sees_mentor_tier_only(matrix):
    mentor, goals = matrix["mentor_ok"], matrix["goals"]
    assert _can_view(mentor, goals[VIS.MENTOR])
    for tier in (VIS.PRIVATE, VIS.OFFICERS, VIS.AGGREGATE_ONLY):
        assert not _can_view(mentor, goals[tier])
    assert set(services.visible_goals(mentor).values_list("pk", flat=True)) == {
        goals[VIS.MENTOR].pk
    }


def test_ended_pairing_admits_nothing(matrix):
    mentor_gone, goals = matrix["mentor_gone"], matrix["goals"]
    for goal in goals.values():
        assert not _can_view(mentor_gone, goal)
    assert services.visible_goals(mentor_gone).count() == 0


# --- anonymous: never anything ------------------------------------------------
def test_anonymous_sees_nothing(matrix):
    anon = AnonymousUser()
    assert services.visible_goals(anon).count() == 0
    assert not services.can_view_goal(anon, matrix["goals"][VIS.MENTOR])


# --- field-level budget + motivation masking (doc 09 §1, §2.3) ---------------
def test_budget_and_motivation_masked_for_non_owner(matrix):
    owner = matrix["owner"]
    goal = _goal(owner, visibility=VIS.OFFICERS, title="shared",
                 budget_isk=Decimal("5000000000"), motivation="I want to lead someday")

    owner_view = services.shared_goal_payload(owner, goal)
    assert owner_view["budget_isk"] == Decimal("5000000000")
    assert owner_view["motivation"] == "I want to lead someday"
    assert services.can_view_budget(owner, goal) is True

    for actor_key in ("officer", "mentor_ok", "member"):
        actor = matrix[actor_key]
        view = services.shared_goal_payload(actor, goal)
        # N-class fields are absent entirely (masking is absence, not a hidden label).
        assert "budget_isk" not in view
        assert "motivation" not in view
        assert "paused_reason" not in view
        assert services.can_view_budget(actor, goal) is False
        # Shared-tier fields still present.
        assert view["title"] == "shared"
        assert view["status"] == goal.status


def test_motivation_never_in_shared_payload_for_any_non_owner(matrix):
    """The motivation sentinel must never appear in a payload shaped for a non-owner viewer."""
    owner = matrix["owner"]
    sentinel = "SENTINEL-MOTIVE-DO-NOT-LEAK"
    goal = _goal(owner, visibility=VIS.OFFICERS, motivation=sentinel)
    for actor_key in ("officer", "director", "mentor_ok", "member", "other"):
        view = services.shared_goal_payload(matrix[actor_key], goal)
        assert sentinel not in str(view)


# ===========================================================================
#  Route-level matrix (doc 09 §9) — the 404 policy, body masking, tier rules
# ===========================================================================
from decimal import Decimal as _D  # noqa: E402

from django.urls import reverse  # noqa: E402

from apps.capsuleer.models import (  # noqa: E402
    CareerMilestone,
    MilestoneKind,
    Verification,
)


def _detail(goal):
    return reverse("capsuleer:goal_detail", args=[goal.pk])


def test_route_owner_sees_all_tiers(client, matrix):
    client.force_login(matrix["owner"])
    for goal in matrix["goals"].values():
        assert client.get(_detail(goal)).status_code == 200


def test_route_member_404_on_every_owner_goal(client, matrix):
    client.force_login(matrix["member"])
    for goal in matrix["goals"].values():
        assert client.get(_detail(goal)).status_code == 404


def test_route_officer_only_officers_tier(client, matrix):
    client.force_login(matrix["officer"])
    assert client.get(_detail(matrix["goals"][VIS.OFFICERS])).status_code == 200
    for tier in (VIS.PRIVATE, VIS.MENTOR, VIS.AGGREGATE_ONLY):
        assert client.get(_detail(matrix["goals"][tier])).status_code == 404


def test_route_officer_read_is_audited(client, matrix):
    from apps.admin_audit.models import AuditLog

    client.force_login(matrix["officer"])
    client.get(_detail(matrix["goals"][VIS.OFFICERS]))
    assert AuditLog.objects.filter(action="capsuleer.goal.view",
                                   target_id=str(matrix["goals"][VIS.OFFICERS].pk)).exists()


def test_route_active_mentor_only_mentor_tier(client, matrix):
    client.force_login(matrix["mentor_ok"])
    assert client.get(_detail(matrix["goals"][VIS.MENTOR])).status_code == 200
    for tier in (VIS.PRIVATE, VIS.OFFICERS, VIS.AGGREGATE_ONLY):
        assert client.get(_detail(matrix["goals"][tier])).status_code == 404


def test_route_ended_pairing_404(client, matrix):
    client.force_login(matrix["mentor_gone"])
    assert client.get(_detail(matrix["goals"][VIS.MENTOR])).status_code == 404


def test_route_missing_pk_and_forbidden_are_both_404(client, matrix):
    client.force_login(matrix["member"])
    missing = reverse("capsuleer:goal_detail", args=[99999999])
    assert client.get(missing).status_code == 404
    assert client.get(_detail(matrix["goals"][VIS.PRIVATE])).status_code == 404


def test_budget_and_motivation_absent_from_non_owner_body(client, django_user_model):
    owner = _member(django_user_model, "bm_owner")
    officer = _officer(django_user_model, "bm_off")
    goal = _goal(owner, visibility=VIS.OFFICERS, title="shared", motivation="SENTINEL_MOTIVE",
                 budget_isk=_D("424242"), paused_reason="SENTINEL_PAUSE")
    client.force_login(officer)
    body = client.get(_detail(goal)).content
    assert b"SENTINEL_MOTIVE" not in body
    assert b"SENTINEL_PAUSE" not in body
    assert b"424242" not in body
    assert b"shared" in body  # title (S-class) is shown


def test_non_owner_mutations_are_404(client, matrix):
    client.force_login(matrix["member"])
    goal = matrix["goals"][VIS.OFFICERS]
    assert client.post(reverse("capsuleer:goal_status", args=[goal.pk]), {"to": "active"}).status_code == 404
    assert client.post(reverse("capsuleer:goal_share", args=[goal.pk]), {"visibility": "private"}).status_code == 404


def test_mentor_note_tier_rules(client, matrix):
    client.force_login(matrix["mentor_ok"])
    # mentor note allowed on the mentor-tier goal, 404 on the private one.
    assert client.post(reverse("capsuleer:goal_note", args=[matrix["goals"][VIS.MENTOR].pk]),
                       {"text": "keep it up"}).status_code == 302
    assert client.post(reverse("capsuleer:goal_note", args=[matrix["goals"][VIS.PRIVATE].pk]),
                       {"text": "x"}).status_code == 404


def test_officer_endorse_officers_tier_audited(client, matrix):
    from apps.admin_audit.models import AuditLog

    goal = matrix["goals"][VIS.OFFICERS]
    ms = CareerMilestone.objects.create(goal=goal, order=1, title="Sign-off",
                                        kind=MilestoneKind.MANUAL, verification=Verification.OFFICER)
    client.force_login(matrix["officer"])
    assert client.post(reverse("capsuleer:goal_endorse", args=[goal.pk]),
                       {"milestone_id": ms.pk}).status_code == 302
    assert AuditLog.objects.filter(action="capsuleer.goal.endorse", target_id=str(goal.pk)).exists()


def test_mentor_cannot_endorse_officers_goal(client, matrix):
    goal = matrix["goals"][VIS.OFFICERS]
    ms = CareerMilestone.objects.create(goal=goal, order=1, title="x",
                                        kind=MilestoneKind.MANUAL, verification=Verification.OFFICER)
    client.force_login(matrix["mentor_ok"])  # a mentor is not an officer
    assert client.post(reverse("capsuleer:goal_endorse", args=[goal.pk]),
                       {"milestone_id": ms.pk}).status_code == 404


def test_leadership_member_403_officer_200(client, matrix):
    client.force_login(matrix["member"])
    assert client.get(reverse("capsuleer:leadership")).status_code == 403
    client.force_login(matrix["officer"])
    assert client.get(reverse("capsuleer:leadership")).status_code == 200


def test_anonymous_redirected_to_login(client, matrix):
    resp = client.get(_detail(matrix["goals"][VIS.MENTOR]))
    assert resp.status_code == 302 and ("/login" in resp["Location"] or "next=" in resp["Location"])
