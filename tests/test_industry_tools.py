"""Phase D: Industry Center tool pages + plan lifecycle (duplicate / archive / visibility)."""
from __future__ import annotations

import pytest

from apps.identity.models import RoleAssignment
from apps.industry.models import IndustryProject, IndustryProjectItem
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db

CRUISER = 600  # T2 Test Cruiser in the SDE sample
RIFTER = 587


def _member(django_user_model, name="pilot", role=rbac.ROLE_MEMBER):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(role))
    return user


# ---- tool pages render -----------------------------------------------------
def test_home_and_guide(client, django_user_model, priced_sde):
    client.force_login(_member(django_user_model))
    assert b"Industry Center" in client.get("/industry/").content
    assert b"How EVE industry works" in client.get("/industry/guide/").content


def test_calculator(client, django_user_model, priced_sde):
    client.force_login(_member(django_user_model))
    empty = client.get("/industry/calculator/")
    assert empty.status_code == 200 and b"Pick an item" in empty.content
    r = client.get(f"/industry/calculator/?type_id={CRUISER}&runs=1")
    assert r.status_code == 200
    est = r.context["estimate"]
    assert est["buildable"] and est["material_cost"] > 0
    assert b"Net profit" in r.content


def test_calculator_folds_invention(client, django_user_model, priced_sde):
    client.force_login(_member(django_user_model))
    r = client.get(f"/industry/calculator/?type_id={CRUISER}&runs=1&invent=1")
    assert r.context["estimate"]["invention_cost"] > 0


def test_invention_planner(client, django_user_model, priced_sde):
    client.force_login(_member(django_user_model))
    r = client.get(f"/industry/invention/?type_id={CRUISER}")
    assert r.status_code == 200 and r.context["plan"]["inventable"]
    assert b"Success chance" in r.content
    # T1 item -> friendly "not invented" message, still 200.
    t1 = client.get(f"/industry/invention/?type_id={RIFTER}")
    assert t1.status_code == 200 and t1.context["plan"] is None


def test_chain_explorer(client, django_user_model, priced_sde):
    client.force_login(_member(django_user_model))
    r = client.get(f"/industry/chain/?type_id={CRUISER}&quantity=1")
    assert r.status_code == 200 and r.context["tree"]["type_id"] == CRUISER
    assert b"Test Cruiser" in r.content


def test_blueprints_and_jobs(client, django_user_model, priced_sde):
    client.force_login(_member(django_user_model))
    assert client.get("/industry/blueprints/").status_code == 200
    j = client.get("/industry/jobs/")
    assert j.status_code == 200 and b"Job tracker" in j.content


def test_corp_demand_and_plan_from_demand(client, django_user_model, priced_sde):
    from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit

    cat = DoctrineCategory.objects.create(key="dps", label="DPS")
    d = Doctrine.objects.create(name="Rifter Fleet", category=cat, status=Doctrine.Status.ACTIVE)
    DoctrineFit.objects.create(doctrine=d, name="Rifter", ship_type_id=RIFTER)
    client.force_login(_member(django_user_model))

    demand = client.get("/industry/demand/")
    assert demand.status_code == 200
    assert any(r["type_id"] == RIFTER for r in demand.context["rows"])

    resp = client.post("/industry/demand/create/", {"type_id": RIFTER, "quantity": 5})
    assert resp.status_code == 302
    plan = IndustryProject.objects.get(source=IndustryProject.Source.DOCTRINE_SUPPLY)
    assert plan.items.get().type_id == RIFTER and plan.items.get().quantity == 5


# ---- plan lifecycle --------------------------------------------------------
def test_duplicate_plan(client, django_user_model, priced_sde):
    user = _member(django_user_model)
    client.force_login(user)
    p = IndustryProject.objects.create(name="Ferox run", created_by=user, assigned_to=user)
    IndustryProjectItem.objects.create(project=p, type_id=CRUISER, quantity=2)
    resp = client.post(f"/industry/plans/{p.pk}/duplicate/")
    assert resp.status_code == 302
    clone = IndustryProject.objects.exclude(pk=p.pk).get()
    assert clone.name == "Copy of Ferox run" and clone.items.count() == 1
    assert clone.status == IndustryProject.Status.DRAFT


def test_archive_and_hidden_from_board(client, django_user_model, priced_sde):
    user = _member(django_user_model)
    client.force_login(user)
    p = IndustryProject.objects.create(name="Old", created_by=user, assigned_to=user)
    client.post(f"/industry/plans/{p.pk}/archive/")
    p.refresh_from_db()
    assert p.is_archived and p.archived_at is not None
    assert p.name.encode() not in client.get("/industry/plans/").content  # gone from board


def test_visibility_hides_private_plan(client, django_user_model, priced_sde):
    owner = _member(django_user_model, name="owner")
    other = _member(django_user_model, name="other")
    p = IndustryProject.objects.create(
        name="SecretBuild", created_by=owner, assigned_to=owner,
        visibility=IndustryProject.Visibility.PRIVATE,
    )
    client.force_login(other)
    assert client.get(f"/industry/plans/{p.pk}/").status_code == 403
    assert b"SecretBuild" not in client.get("/industry/plans/").content
    # Owner still sees it.
    client.force_login(owner)
    assert client.get(f"/industry/plans/{p.pk}/").status_code == 200


def test_officer_sees_archived_toggle(client, django_user_model, priced_sde):
    officer = _member(django_user_model, name="off", role=rbac.ROLE_OFFICER)
    IndustryProject.objects.create(name="ArchivedOne", is_archived=True)
    client.force_login(officer)
    assert b"ArchivedOne" in client.get("/industry/plans/?archived=1").content
