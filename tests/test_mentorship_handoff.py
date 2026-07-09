"""MEN-2 — onboarding → mentorship handoff worklist."""
from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.mentorship.models import MenteeProfile
from apps.mentorship.onboarding_handoff import handoff_candidates
from apps.onboarding.models import OnboardingMilestone, OnboardingProgress
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db


@pytest.fixture
def milestones():
    OnboardingMilestone.objects.all().delete()  # controlled set (test is transactional)
    return [
        OnboardingMilestone.objects.create(key=f"m{i}", title=f"M{i}", active=True)
        for i in range(2)
    ]


def _pilot(django_user_model, cid, name=None):
    user = django_user_model.objects.create(username=f"eve:{cid}")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    ch = EveCharacter.objects.create(character_id=cid, user=user, name=name or f"P{cid}",
                                     is_main=True, is_corp_member=True)
    return user, ch


def _complete(ch, milestones, when=None):
    for m in milestones:
        OnboardingProgress.objects.create(character=ch, milestone=m,
                                          status=OnboardingProgress.Status.DONE,
                                          completed_at=when or timezone.now())


def _users_in(handoff):
    return {h["user"].id for h in handoff}


def test_completed_cadet_surfaces(django_user_model, milestones):
    user, ch = _pilot(django_user_model, 7001)
    _complete(ch, milestones)
    handoff = handoff_candidates()
    assert user.id in _users_in(handoff)
    assert next(h for h in handoff if h["user"].id == user.id)["state"] == "complete"


def test_existing_mentee_excluded(django_user_model, milestones):
    user, ch = _pilot(django_user_model, 7002)
    _complete(ch, milestones)
    MenteeProfile.objects.create(user=user)
    assert user.id not in _users_in(handoff_candidates())


def test_stalled_cadet_surfaces(django_user_model, milestones):
    user, ch = _pilot(django_user_model, 7003)
    # one milestone done 20 days ago, the other never → stalled
    OnboardingProgress.objects.create(character=ch, milestone=milestones[0],
                                      status=OnboardingProgress.Status.DONE,
                                      completed_at=timezone.now() - dt.timedelta(days=20))
    OnboardingProgress.objects.create(character=ch, milestone=milestones[1],
                                      status=OnboardingProgress.Status.TODO)
    handoff = handoff_candidates()
    assert next(h for h in handoff if h["user"].id == user.id)["state"] == "stalled"


def test_active_cadet_not_surfaced(django_user_model, milestones):
    user, ch = _pilot(django_user_model, 7004)
    # one milestone done recently, still progressing → not a handoff
    OnboardingProgress.objects.create(character=ch, milestone=milestones[0],
                                      status=OnboardingProgress.Status.DONE,
                                      completed_at=timezone.now())
    OnboardingProgress.objects.create(character=ch, milestone=milestones[1],
                                      status=OnboardingProgress.Status.TODO)
    assert user.id not in _users_in(handoff_candidates())


def test_deactivated_milestone_does_not_count_as_complete(django_user_model, milestones):
    user, ch = _pilot(django_user_model, 7009)
    # a since-deactivated milestone the cadet completed must NOT count toward completion
    dead = OnboardingMilestone.objects.create(key="dead", title="Dead", active=False)
    _complete(ch, [milestones[0], dead])  # one active + one inactive done, milestones[1] not
    assert user.id not in _users_in(handoff_candidates())  # still has an active milestone left


def test_non_corp_member_excluded(django_user_model, milestones):
    user = django_user_model.objects.create(username="eve:7005")
    ch = EveCharacter.objects.create(character_id=7005, user=user, name="X",
                                     is_main=True, is_corp_member=False)
    _complete(ch, milestones)
    assert user.id not in _users_in(handoff_candidates())


def test_dedup_by_account(django_user_model, milestones):
    user, ch1 = _pilot(django_user_model, 7006)
    ch2 = EveCharacter.objects.create(character_id=7007, user=user, name="Alt",
                                      is_corp_member=True)
    _complete(ch1, milestones)
    _complete(ch2, milestones)
    handoff = handoff_candidates()
    assert sum(1 for h in handoff if h["user"].id == user.id) == 1  # one row per account


def test_matching_page_shows_handoff(client, django_user_model, milestones):
    user, ch = _pilot(django_user_model, 7008, name="Rookie")
    _complete(ch, milestones)
    officer = django_user_model.objects.create(username="officer")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    client.force_login(officer)
    resp = client.get(reverse("admin_audit:mentorship_matching"))
    assert resp.status_code == 200
    assert b"Onboarding cadets not yet in mentorship" in resp.content
