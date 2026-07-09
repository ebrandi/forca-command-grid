"""PI-1 (roadmap 3.15) — colony-to-plan reconciliation.

Match the plan owner's live imported colonies to each plan planet and flag drift (missing
colony, wrong product, or a colony with issues + the derivable cause).
"""
from __future__ import annotations

import pytest
from django.utils import timezone

from apps.planetary.models import PiColony, PiMaterial, PiPlan, PiPlanetType
from apps.planetary.services import reconcile_plan_colonies
from apps.sso.models import EveCharacter
from core.mixins import Source
from tests.test_planetary import _member

pytestmark = pytest.mark.django_db


@pytest.fixture
def pi_static(db):
    from django.core.management import call_command

    call_command("load_pi_static")


def _plan(user):
    barren = PiPlanetType.objects.get(slug="barren")
    p0 = PiMaterial.objects.filter(tier="P0").first()
    plan = PiPlan.objects.create(owner=user, name="Barren")
    plan.planets.create(planet_type=barren, role="extract", primary_material=p0, order=0)
    return plan, barren, p0


def _colony(user, planet_type_name, *, extracting=None, issues=None):
    char = EveCharacter.objects.get(user=user)
    return PiColony.objects.create(
        character=char, planet_id=1, planet_type_name=planet_type_name,
        solar_system_name="Amamake",
        summary={"extracting": extracting or [], "issues": issues or []},
        source=Source.ESI_CHAR, as_of=timezone.now(), fetched_at=timezone.now(),
    )


def test_no_colonies_returns_empty(django_user_model, pi_static):
    user = _member(django_user_model, "a", cid=991000)
    plan, _b, _p = _plan(user)
    assert reconcile_plan_colonies(plan) == []


def test_matched_colony_is_ok(django_user_model, pi_static):
    user = _member(django_user_model, "b", cid=991001)
    plan, barren, p0 = _plan(user)
    _colony(user, barren.name, extracting=[{"type_id": p0.type_id}])
    rows = reconcile_plan_colonies(plan)
    assert len(rows) == 1
    assert rows[0]["colony"] is not None and rows[0]["ok"] is True and rows[0]["drift"] == ""


def test_missing_colony_flagged(django_user_model, pi_static):
    user = _member(django_user_model, "c", cid=991002)
    plan, _b, _p = _plan(user)
    _colony(user, "Gas", extracting=[])  # a colony of a different type
    rows = reconcile_plan_colonies(plan)
    assert rows[0]["colony"] is None and "No live colony" in rows[0]["drift"]


def test_issue_flagged_with_cause(django_user_model, pi_static):
    user = _member(django_user_model, "d", cid=991003)
    plan, barren, p0 = _plan(user)
    _colony(user, barren.name, extracting=[{"type_id": p0.type_id}], issues=["Extractor expired"])
    rows = reconcile_plan_colonies(plan)
    assert rows[0]["ok"] is False and rows[0]["drift"] == "Extractor expired"


def test_wrong_product_flagged(django_user_model, pi_static):
    user = _member(django_user_model, "e", cid=991004)
    plan, barren, p0 = _plan(user)
    other = PiMaterial.objects.filter(tier="P0").exclude(type_id=p0.type_id).first()
    _colony(user, barren.name, extracting=[{"type_id": other.type_id}])
    rows = reconcile_plan_colonies(plan)
    assert rows[0]["ok"] is False and "different product" in rows[0]["drift"]


def test_panel_is_owner_only_not_leaked_to_corp_viewer(client, django_user_model, pi_static):
    from django.urls import reverse

    owner = _member(django_user_model, "own", cid=991005)
    plan, barren, p0 = _plan(owner)
    plan.visibility = "corp"
    plan.save(update_fields=["visibility"])
    _colony(owner, barren.name, extracting=[{"type_id": p0.type_id}])

    # A different member can view the corp plan but must NOT see the owner's live colonies.
    viewer = _member(django_user_model, "viewer", cid=991006)
    client.force_login(viewer)
    resp = client.get(reverse("planetary:detail", args=[plan.pk]))
    assert resp.status_code == 200
    assert b"Your live colonies vs this plan" not in resp.content

    # The owner sees their own reconciliation.
    client.force_login(owner)
    resp = client.get(reverse("planetary:detail", args=[plan.pk]))
    assert b"Your live colonies vs this plan" in resp.content
