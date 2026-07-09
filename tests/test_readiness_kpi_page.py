"""Milestone — G4 (the per-KPI drill-down page, history-backed by the FR4 snapshot column)."""
from __future__ import annotations

import pytest

from apps.identity.models import RoleAssignment
from apps.readiness.models import MandatoryShip, ReadinessSnapshot
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

RIFTER = 587
KPI = "strategic.mandatory_ship_coverage"


def _officer(django_user_model, name="off"):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    return user


def _corp_member(cid=7001):
    return EveCharacter.objects.create(character_id=cid, name=f"P{cid}", is_main=True, is_corp_member=True)


@pytest.mark.django_db
def test_kpi_page_renders_with_trend(client, django_user_model, sde):
    _corp_member()
    MandatoryShip.objects.create(label="Rifter", ship_type_id=RIFTER, required_quantity=1)
    # A little history so the trend bars render.
    ReadinessSnapshot.objects.create(index=60, dimensions={"strategic": 50},
                                     kpis={KPI: {"value": 0.5, "score": 50, "status": "amber"}})
    client.force_login(_officer(django_user_model))
    html = client.get(f"/readiness/kpi/{KPI}/").content.decode()
    assert KPI in html
    assert "Score history" in html
    assert "Strategic Assets" in html  # parent dimension label


@pytest.mark.django_db
def test_kpi_page_404_for_unknown_dimension(client, django_user_model, sde):
    client.force_login(_officer(django_user_model, "off2"))
    assert client.get("/readiness/kpi/nosuch.thing/").status_code == 404


@pytest.mark.django_db
def test_kpi_page_404_for_unknown_kpi_in_real_dimension(client, django_user_model, sde):
    _corp_member()
    MandatoryShip.objects.create(label="Rifter", ship_type_id=RIFTER, required_quantity=1)
    client.force_login(_officer(django_user_model, "off3"))
    assert client.get("/readiness/kpi/strategic.not_a_kpi/").status_code == 404


@pytest.mark.django_db
def test_kpi_page_is_officer_only(client, django_user_model, sde):
    member = django_user_model.objects.create(username="m")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    assert client.get(f"/readiness/kpi/{KPI}/").status_code == 403
