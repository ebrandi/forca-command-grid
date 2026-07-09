"""REC-KB-3 (roadmap 3.16) — candidate-to-member handoff.

Marking a candidate *joined* routes a linked pilot into onboarding (checklist) and mentorship
(registered mentee, surfaced to matching), carrying the vetting notes across. A no-op with a
clear reason when the pilot hasn't signed into FORCA yet.
"""
from __future__ import annotations

import pytest

from apps.recruitment.models import Candidate
from apps.recruitment.services import handoff_joined_candidate
from apps.sso.models import EveCharacter

pytestmark = pytest.mark.django_db


def _candidate(cid, notes="Solid scout"):
    return Candidate.objects.create(character_id=cid, name=f"Cand {cid}", notes=notes)


def _linked(django_user_model, cid, username):
    user = django_user_model.objects.create(username=username)
    EveCharacter.objects.create(character_id=cid, user=user, name=username, is_main=True,
                                is_corp_member=True)
    return user


def test_no_account_is_noop():
    cand = _candidate(88001)
    assert handoff_joined_candidate(cand) == {"handed_off": False, "reason": "no_account"}


def test_joined_with_account_routes_to_onboarding_and_mentorship(django_user_model):
    user = _linked(django_user_model, 88002, "joiner")
    cand = _candidate(88002, notes="Great FC — but ex-Goon, verify loyalty")
    result = handoff_joined_candidate(cand)
    assert result["handed_off"] is True
    assert result["onboarding_started"] is True
    assert result["mentee_created"] is True

    from apps.mentorship.models import MenteeProfile
    mp = MenteeProfile.objects.get(user=user)
    # Private recruiter notes must NOT leak into the mentee's own (self-visible) profile.
    assert "ex-Goon" not in mp.notes and mp.notes == ""
    # The vetting context is preserved on the officer-only Candidate record.
    cand.refresh_from_db()
    assert "ex-Goon" in cand.notes


def test_handoff_idempotent_when_already_mentee(django_user_model):
    from apps.mentorship.models import MenteeProfile

    user = _linked(django_user_model, 88003, "already")
    MenteeProfile.objects.create(user=user)  # already in the mentee system
    cand = _candidate(88003)
    result = handoff_joined_candidate(cand)
    assert result["handed_off"] is True and result["mentee_created"] is False
