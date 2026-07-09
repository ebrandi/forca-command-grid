"""Raffle eligibility — the single enrolment enforcement point.

Only an enrolled pilot with a live, non-revoked ESI token who is a recognised corp
pilot and not excluded may earn tickets / win. Every other state is ineligible with
an explainable reason code that maps to a ``RaffleIneligibleActivity.Reason``.
"""
from __future__ import annotations

import pytest
from django.utils import timezone

from apps.raffle import eligibility as elig
from apps.raffle import services
from apps.sso.models import EveCharacter
from core import rbac
from tests._raffle_utils import (
    add_token,
    detached_character,
    enrol_pilot,
    make_contest,
    make_user,
)


@pytest.mark.django_db
def test_enrolled_pilot_with_valid_token_is_eligible(django_user_model):
    contest = make_contest(seed_sources=False)
    enrol_pilot(django_user_model, 1001)
    e = elig.for_character_id(contest, 1001)
    assert e.eligible is True
    assert e.enrolled and e.has_valid_token and e.is_corp_member
    assert e.esi_status == elig.ESI_VALID
    assert e.reason_code == ""


@pytest.mark.django_db
def test_not_enrolled_no_character_row(django_user_model):
    """A raw killmail character with no EveCharacter at all → not enrolled."""
    contest = make_contest(seed_sources=False)
    e = elig.for_character_id(contest, 424242)
    assert e.eligible is False
    assert e.reason_code == "not_enrolled"
    assert e.esi_status == elig.ESI_NONE


@pytest.mark.django_db
def test_detached_character_is_not_enrolled(django_user_model):
    """An EveCharacter linked to no User (never claimed / erased)."""
    contest = make_contest(seed_sources=False)
    detached_character(1002)
    e = elig.for_character_id(contest, 1002)
    assert e.eligible is False
    assert e.enrolled is False
    assert e.reason_code == "not_enrolled"


@pytest.mark.django_db
def test_revoked_token_is_ineligible(django_user_model):
    contest = make_contest(seed_sources=False)
    _, character = enrol_pilot(django_user_model, 1003)
    token = character.tokens.first()
    token.revoked_at = timezone.now()
    token.save(update_fields=["revoked_at"])

    e = elig.for_character_id(contest, 1003)
    assert e.eligible is False
    assert e.has_valid_token is False
    assert e.esi_status == elig.ESI_REVOKED
    # Revoked (not "never connected") surfaces as token_expired.
    assert e.reason_code == "token_expired"


@pytest.mark.django_db
def test_no_token_is_ineligible(django_user_model):
    contest = make_contest(seed_sources=False)
    enrol_pilot(django_user_model, 1004, with_token=False)
    e = elig.for_character_id(contest, 1004)
    assert e.eligible is False
    assert e.has_valid_token is False
    assert e.esi_status == elig.ESI_NONE
    assert e.reason_code == "no_token"


@pytest.mark.django_db
def test_not_corp_member_is_ineligible(django_user_model):
    contest = make_contest(seed_sources=False)
    enrol_pilot(django_user_model, 1005, is_corp_member=False)
    e = elig.for_character_id(contest, 1005)
    assert e.eligible is False
    assert e.is_corp_member is False
    assert e.reason_code == "not_corp"


@pytest.mark.django_db
def test_excluded_pilot_is_ineligible(django_user_model):
    contest = make_contest(seed_sources=False)
    user, _ = enrol_pilot(django_user_model, 1006)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    services.exclude_pilot(contest, director, user=user, reason="boosting")

    e = elig.for_character_id(contest, 1006)
    assert e.eligible is False
    assert e.excluded is True
    assert e.reason_code == "excluded"


@pytest.mark.django_db
def test_missing_required_scope_is_ineligible(django_user_model):
    """A contest can demand extra scopes the pilot's token doesn't carry."""
    contest = make_contest(seed_sources=False,
                           required_scopes=["esi-killmails.read_corporation_killmails.v1"])
    enrol_pilot(django_user_model, 1007, scopes=["publicData"])
    e = elig.for_character_id(contest, 1007)
    assert e.eligible is False
    assert e.scopes_ok is False
    assert e.reason_code == "missing_scope"
    assert "esi-killmails.read_corporation_killmails.v1" in e.missing_scopes


@pytest.mark.django_db
def test_for_user_prefers_an_eligible_character(django_user_model):
    """An account is eligible if ANY of its characters is."""
    contest = make_contest(seed_sources=False)
    user, main = enrol_pilot(django_user_model, 1008, with_token=False)  # main has no token
    alt = EveCharacter.objects.create(character_id=10081, user=user, name="Alt",
                                      is_main=False, is_corp_member=True)
    add_token(alt)  # the alt is fully valid
    e = elig.for_user(contest, user)
    assert e.eligible is True
