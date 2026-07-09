"""Milestone — G9 (snapshot per-KPI column populated) + G10 (officer view of a pilot).

G9: the pipeline payload now carries a ``kpis`` map, so a persisted ReadinessSnapshot
records KPI-level scores for future trend/forecast use (PRD FR4).
G10: officers get a read-only ``/readiness/me/<character_id>/`` coaching view.
"""
from __future__ import annotations

import pytest

from apps.characters.models import CharacterSkillSnapshot
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit, SkillRequirement
from apps.identity.models import RoleAssignment
from apps.readiness.models import ReadinessSnapshot
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

GUNNERY = 3300
RIFTER = 587


def _user(django_user_model, name, role):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(role))
    return user


def _pilot(django_user_model, cid=9001, gunnery_level=5):
    user = django_user_model.objects.create(username=f"eve:{cid}")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    ch = EveCharacter.objects.create(
        character_id=cid, user=user, name=f"P{cid}", is_main=True, is_corp_member=True
    )
    CharacterSkillSnapshot.objects.create(
        character=ch, is_latest=True, skills={str(GUNNERY): {"trained_level": gunnery_level, "sp": 0}}
    )
    return user, ch


def _doctrine(name="Core", req_level=3):
    cat, _ = DoctrineCategory.objects.get_or_create(key="dps", label="DPS")
    d = Doctrine.objects.create(name=name, category=cat, priority=100)
    fit = DoctrineFit.objects.create(doctrine=d, name=name, ship_type_id=RIFTER)
    SkillRequirement.objects.create(fit=fit, skill_type_id=GUNNERY, min_level=req_level, optimal_level=req_level)
    return d


# --- G9: snapshot kpis populated ---------------------------------------------
@pytest.mark.django_db
def test_payload_carries_kpis_and_snapshot_persists_them(django_user_model, sde):
    from apps.readiness.services import compute_readiness

    _doctrine("Core", 3)
    _pilot(django_user_model, gunnery_level=5)
    payload = compute_readiness(persist=True, use_cache=False)
    assert "kpis" in payload and isinstance(payload["kpis"], dict)
    # Each KPI entry carries score/value/status.
    if payload["kpis"]:
        sample = next(iter(payload["kpis"].values()))
        assert {"value", "score", "status"} <= set(sample)
    snap = ReadinessSnapshot.objects.latest("created_at")
    assert snap.kpis == payload["kpis"]  # the persisted column is no longer empty {}


# --- G10: officer view of a pilot --------------------------------------------
@pytest.mark.django_db
def test_officer_can_view_pilot_quest_log(client, django_user_model, sde):
    _doctrine("Core", 3)
    _, ch = _pilot(django_user_model, cid=9001, gunnery_level=1)  # trainable → has quests
    officer = _user(django_user_model, "off", rbac.ROLE_OFFICER)
    client.force_login(officer)
    html = client.get(f"/readiness/me/{ch.character_id}/").content.decode()
    assert "officer view" in html
    assert f"{ch.name}'s readiness" in html
    # Read-only: no action forms posting to reco_action.
    assert "reco/" not in html


@pytest.mark.django_db
def test_pilot_view_is_officer_only(client, django_user_model, sde):
    _, ch = _pilot(django_user_model, cid=9001)
    member = _user(django_user_model, "m", rbac.ROLE_MEMBER)
    client.force_login(member)
    assert client.get(f"/readiness/me/{ch.character_id}/").status_code == 403


@pytest.mark.django_db
def test_pilot_view_404_for_non_corp_member(client, django_user_model, sde):
    officer = _user(django_user_model, "off2", rbac.ROLE_OFFICER)
    client.force_login(officer)
    assert client.get("/readiness/me/999999/").status_code == 404
