"""Capsuleer Path query budgets (doc 14). Page renders stay within a fixed query ceiling so a goal
list, milestone list, endorsement stream, activity history or mentor pool never fans out into an
N+1. The fixtures deliberately seed the worst realistic shape (Stage 5 finding 43) so the ceiling
actually exercises the fan-out paths, not the cheap empty case."""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.capsuleer import services
from apps.capsuleer.models import CareerActionStep, GoalStatus, GoalType, Verification
from apps.capsuleer.templates_builtin import sync_builtin_templates

from ._capsuleer_utils import _character, _member, _milestone

pytestmark = pytest.mark.django_db


def _pilot(client, django_user_model, cid=42001):
    user = _member(django_user_model, str(cid))
    _character(user, cid, "Perf Pilot")
    client.force_login(user)
    return user


def test_home_query_budget(client, django_user_model, django_assert_max_num_queries):
    user = _pilot(client, django_user_model)
    for i in range(4):
        services.create_goal(user, title=f"g{i}", goal_type=GoalType.CUSTOM,
                             status=GoalStatus.ACTIVE)
    client.get(reverse("capsuleer:home"))  # warm caches/session
    with django_assert_max_num_queries(15):
        client.get(reverse("capsuleer:home"))


def test_goal_detail_query_budget(client, django_user_model, django_assert_max_num_queries):
    """The doc's ≤20 warmed budget against a realistic goal: mentor/officer-verified milestones with
    endorsement history and a deep activity stream from two actors (finding 43)."""
    user = _pilot(client, django_user_model)
    char = next(iter(user.characters.all()))
    other = _member(django_user_model, "perf_actor2")
    goal = services.create_goal(user, title="Fly logi", goal_type=GoalType.CUSTOM, character=char,
                                status=GoalStatus.ACTIVE)
    for i in range(6):
        _milestone(goal, title=f"self{i}", verification=Verification.SELF)
    m_mentor = _milestone(goal, title="reps signed off", verification=Verification.MENTOR)
    m_off = _milestone(goal, title="fc signoff", verification=Verification.OFFICER)
    services.record_activity(goal, other, services.V_ENDORSED,
                             {"milestone_id": m_mentor.pk, "role": "mentor"})
    services.record_activity(goal, other, services.V_ENDORSED,
                             {"milestone_id": m_off.pk, "role": "officer"})
    for i in range(16):
        services.record_activity(goal, other if i % 2 else user, "goal.edited", {"n": i})
    for i in range(4):
        CareerActionStep.objects.create(goal=goal, title=f"s{i}", source="pilot")
    url = reverse("capsuleer:goal_detail", args=[goal.pk])
    client.get(url)  # warm
    with django_assert_max_num_queries(20):
        client.get(url)


def test_browse_query_budget(client, django_user_model, django_assert_max_num_queries):
    _pilot(client, django_user_model)
    sync_builtin_templates()
    client.get(reverse("capsuleer:paths"))  # warm (also warms the cached corp-demand set)
    with django_assert_max_num_queries(10):
        client.get(reverse("capsuleer:paths"))


def test_compare_query_budget(client, django_user_model, django_assert_max_num_queries):
    """Compare is bounded and does NOT scale with the mentor pool — the mentor count is one annotated
    fetch, not a COUNT per mentor per template (finding 24/43)."""
    from apps.mentorship.models import MentorProfile, MentorshipTrack

    _pilot(client, django_user_model)
    sync_builtin_templates()
    areas = [MentorshipTrack.Category.PVP, MentorshipTrack.Category.LOGISTICS,
             MentorshipTrack.Category.EXPLORATION]
    for i in range(6):
        u = _member(django_user_model, f"perf_mentor{i}")
        MentorProfile.objects.create(user=u, status=MentorProfile.Status.ACTIVE, areas=areas,
                                     max_active_mentees=5)
    keys = "tackle_pilot,explorer"
    client.get(reverse("capsuleer:compare"), {"keys": keys})  # warm
    with django_assert_max_num_queries(14):
        client.get(reverse("capsuleer:compare"), {"keys": keys})


def test_leadership_query_budget(client, django_user_model, django_assert_max_num_queries):
    from ._capsuleer_utils import _goal, _officer

    officer = _officer(django_user_model, "perf_off")
    for i in range(6):
        owner = _member(django_user_model, f"pp{i}")
        _goal(owner, visibility="officers", title=f"shared {i}")
    client.force_login(officer)
    client.get(reverse("capsuleer:leadership"))  # warm
    with django_assert_max_num_queries(12):
        client.get(reverse("capsuleer:leadership"))


def test_mentor_count_reads_program_once(django_user_model, django_assert_max_num_queries):
    """The mentor-count helper reads the programme's default capacity once, not once per mentor —
    mentors with the default ``max_active_mentees=0`` used to trigger an uncached program SELECT each
    (finding 23 residual)."""
    from apps.capsuleer.views import _mentor_counts_for_categories
    from apps.mentorship.models import MentorProfile, MentorshipTrack

    for i in range(6):
        u = _member(django_user_model, f"nullcap_mentor{i}")
        MentorProfile.objects.create(user=u, status=MentorProfile.Status.ACTIVE,
                                     areas=[MentorshipTrack.Category.PVP])  # max_active_mentees = 0
    with django_assert_max_num_queries(4):
        _mentor_counts_for_categories({"tackle_scout"})


def test_dashboard_bundle_delta(client, django_user_model, django_assert_max_num_queries):
    user = _pilot(client, django_user_model)
    for i in range(3):
        goal = services.create_goal(user, title=f"g{i}", goal_type=GoalType.CUSTOM,
                                    status=GoalStatus.ACTIVE)
        CareerActionStep.objects.create(goal=goal, title=f"step{i}", source="pilot")
    # The panel and the quest row share one active-goal fetch, so the whole dashboard capsuleer
    # delta stays within the doc's ≤3-query budget (finding 22).
    with django_assert_max_num_queries(3):
        services.dashboard_bundle(user)
