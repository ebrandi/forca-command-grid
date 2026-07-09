"""4.11 — Fleet simulator availability-aware mode.

Acceptance: the simulator can count only recently-active pilots ("who'll realistically
show up") instead of the whole qualified roster, via an aggregate login-recency signal
(activity, never per-pilot presence surveillance). Full-roster mode is unchanged.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.characters.models import CharacterSkillSnapshot
from apps.corporation.models import CorpMember
from apps.identity.models import RoleAssignment
from apps.readiness.dimensions.roles import active_member_ids, qualified_count
from apps.readiness.models import StrategicRoleTarget
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db
GUNNERY = 3300
SIM_URL = "/readiness/sim/"


def _officer(django_user_model, name="off"):
    u = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_OFFICER))
    return u


def _member(django_user_model, cid, *, gunnery=5, logon_days_ago=None):
    u = django_user_model.objects.create(username=f"eve:{cid}")
    ch = EveCharacter.objects.create(
        character_id=cid, name=f"P{cid}", is_main=True, is_corp_member=True, user=u
    )
    CharacterSkillSnapshot.objects.create(
        character=ch, is_latest=True, skills={str(GUNNERY): {"trained_level": gunnery}}
    )
    if logon_days_ago is not None:
        CorpMember.objects.create(
            character_id=cid, corporation_id=98000001,
            logon_date=timezone.now() - dt.timedelta(days=logon_days_ago),
        )
    return ch


def _target(desired=3, level=5):
    return StrategicRoleTarget.objects.create(
        role_key="logi", label="Logistics", desired_count=desired,
        detection=StrategicRoleTarget.Detection.SKILLS,
        detection_params={"skills": {str(GUNNERY): level}},
    )


def test_active_member_ids_respects_window():
    CorpMember.objects.create(character_id=1, corporation_id=98000001,
                              logon_date=timezone.now() - dt.timedelta(days=3))
    CorpMember.objects.create(character_id=2, corporation_id=98000001,
                              logon_date=timezone.now() - dt.timedelta(days=60))
    CorpMember.objects.create(character_id=3, corporation_id=98000001, logon_date=None)
    assert active_member_ids(30) == {1}
    assert active_member_ids(90) == {1, 2}


def test_qualified_count_restriction(django_user_model):
    _member(django_user_model, 9001, gunnery=5)
    _member(django_user_model, 9002, gunnery=5)
    t = _target(desired=3, level=5)
    assert qualified_count(t) == 2                        # whole roster
    assert qualified_count(t, only_char_ids={9001}) == 1  # restricted to one
    assert qualified_count(t, only_char_ids=set()) == 0   # nobody active


def test_simulator_available_mode_counts_only_active(client, django_user_model):
    _member(django_user_model, 9001, gunnery=5, logon_days_ago=2)   # active
    _member(django_user_model, 9002, gunnery=5, logon_days_ago=90)  # stale login
    _target(desired=3, level=5)
    client.force_login(_officer(django_user_model))

    full = client.get(SIM_URL)
    assert full.context["available"] is False
    assert full.context["total_fieldable"] == 2  # whole qualified roster

    avail = client.get(SIM_URL + "?available=1")
    assert avail.context["available"] is True
    assert avail.context["active_count"] == 1
    assert avail.context["total_fieldable"] == 1  # only the recently-active pilot
    assert avail.context["window_days"] == 30


def test_simulator_toggle_and_framing_render(client, django_user_model):
    _member(django_user_model, 9101, gunnery=5, logon_days_ago=1)
    _target(desired=2, level=5)
    client.force_login(_officer(django_user_model))
    html = client.get(SIM_URL + "?available=1").content.decode()
    assert "Likely available" in html and "Full roster" in html
    assert "not presence tracking" in html  # constructive framing, no surveillance


def test_malformed_window_config_falls_back_to_default(client, django_user_model):
    from apps.admin_audit.models import AppSetting
    AppSetting.objects.create(key="readiness.sim_availability_days", value="not-a-number")
    _member(django_user_model, 9301, gunnery=5, logon_days_ago=1)
    _target(desired=1, level=5)
    client.force_login(_officer(django_user_model))
    r = client.get(SIM_URL + "?available=1")
    assert r.status_code == 200 and r.context["window_days"] == 30  # bad config → default


def test_full_roster_is_backward_compatible(client, django_user_model):
    # No CorpMember rows at all → full roster still counts skill-qualified pilots.
    _member(django_user_model, 9201, gunnery=5)
    _member(django_user_model, 9202, gunnery=5)
    _target(desired=2, level=5)
    client.force_login(_officer(django_user_model))
    r = client.get(SIM_URL)
    assert r.context["total_fieldable"] == 2 and r.context["verdict"] == "ready"
