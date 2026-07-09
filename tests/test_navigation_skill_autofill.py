"""4.1 — Skill-aware jump planner.

Acceptance: the planner auto-fills JDC/JFC/JF from the pilot's OWN already-synced
skills (least-privilege reuse — no new scope) instead of leadership's generic
defaults; an explicit query param still overrides; an anonymous pilot or one without
a skill snapshot falls back to the config defaults.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.characters.models import CharacterSkillSnapshot
from apps.navigation.jump_service import jump_skills_for_user
from apps.navigation.models import JumpPlannerConfig
from tests._raffle_utils import enrol_pilot

pytestmark = pytest.mark.django_db

# JDC=21611, JFC=21610, JF=29029 (verified against the SDE).
def _snapshot(character, *, jdc=4, jfc=3, jf=5):
    CharacterSkillSnapshot.objects.create(
        character=character, is_latest=True,
        skills={
            "21611": {"trained_level": jdc, "sp": 100},
            "21610": {"trained_level": jfc, "sp": 100},
            "29029": {"trained_level": jf, "sp": 100},
        },
    )


def test_jump_skills_for_user_reads_latest_snapshot(django_user_model):
    user, char = enrol_pilot(django_user_model, 7001)
    _snapshot(char, jdc=4, jfc=3, jf=5)
    skills = jump_skills_for_user(user)
    assert skills["jdc"] == 4 and skills["jfc"] == 3 and skills["jf"] == 5
    assert skills["character"].character_id == 7001


def test_jump_skills_none_without_snapshot(django_user_model):
    user, _char = enrol_pilot(django_user_model, 7002)
    assert jump_skills_for_user(user) is None


def test_jump_skills_none_for_anonymous():
    from django.contrib.auth.models import AnonymousUser
    assert jump_skills_for_user(AnonymousUser()) is None


def test_untrained_skill_reads_zero(django_user_model):
    user, char = enrol_pilot(django_user_model, 7003)
    CharacterSkillSnapshot.objects.create(
        character=char, is_latest=True, skills={"21611": {"trained_level": 2}},
    )
    skills = jump_skills_for_user(user)
    assert skills["jdc"] == 2 and skills["jfc"] == 0 and skills["jf"] == 0  # untrained -> 0


def test_view_autofills_defaults_from_pilot_skills(client, django_user_model):
    user, char = enrol_pilot(django_user_model, 7004)
    _snapshot(char, jdc=4, jfc=3, jf=5)
    client.force_login(user)
    resp = client.get(reverse("navigation:jump_planner"))
    assert resp.status_code == 200
    assert resp.context["jdc"] == 4 and resp.context["jfc"] == 3 and resp.context["jf_skill"] == 5
    assert resp.context["pilot_skills"] is not None
    assert b"pre-filled from" in resp.content


def test_view_explicit_param_overrides_autofill(client, django_user_model):
    user, char = enrol_pilot(django_user_model, 7005)
    _snapshot(char, jdc=4)
    client.force_login(user)
    resp = client.get(reverse("navigation:jump_planner") + "?jdc=2")
    assert resp.status_code == 200
    assert resp.context["jdc"] == 2  # explicit choice wins over the auto-fill


def test_view_falls_back_to_config_default_without_skills(client, django_user_model):
    cfg = JumpPlannerConfig.active()
    user, _char = enrol_pilot(django_user_model, 7006)  # no snapshot
    client.force_login(user)
    resp = client.get(reverse("navigation:jump_planner"))
    assert resp.status_code == 200
    assert resp.context["jdc"] == cfg.default_jdc
    assert resp.context["pilot_skills"] is None
