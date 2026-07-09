"""Member-facing interaction tests: industry, logistics, stock, skill plans.

Covers the happy path plus the access-control boundaries (anonymous redirect,
non-member 403, and "not yours" ownership checks) for the new write actions.
"""
from __future__ import annotations

import pytest

from apps.characters.models import CharacterSkillSnapshot
from apps.doctrines.models import Doctrine, DoctrineFit
from apps.doctrines.services import derive_skill_requirements
from apps.industry.models import IndustryProject
from apps.market.models import MarketLocation
from apps.skills.models import SkillPlan, SkillPlanStep
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from apps.stockpile.models import HaulingTask, Stockpile
from core import rbac


def make_member(django_user_model, username, char_id, role=rbac.ROLE_MEMBER):
    from apps.identity.models import RoleAssignment

    user = django_user_model.objects.create(username=username, first_name=username.title())
    RoleAssignment.objects.create(user=user, role=ensure_role(role))
    char = EveCharacter.objects.create(
        character_id=char_id, user=user, name=f"{username} pilot", is_main=True, is_corp_member=True
    )
    return user, char


# --- Industry -----------------------------------------------------------------
@pytest.mark.django_db
def test_member_creates_and_manages_project(client, django_user_model, sde):
    user, _ = make_member(django_user_model, "alice", 2001)
    client.force_login(user)

    resp = client.post("/industry/plans/new/", {
        "name": "Build a Rifter", "objective_type": "build", "description": "",
        "type_id": 587, "quantity": 2, "build_or_buy": "build",
        "strategy": "build_to_minerals", "me": 0,
    })
    assert resp.status_code == 302
    project = IndustryProject.objects.get(name="Build a Rifter")
    assert project.assigned_to_id == user.id
    assert project.status == IndustryProject.Status.ACTIVE
    # BOM was computed: Rifter expands to minerals.
    assert project.items.first().material_requirements.exists()

    # Add a second item, then remove it.
    client.post(f"/industry/plans/{project.pk}/items/add/", {
        "type_id": 600, "quantity": 1, "build_or_buy": "build", "strategy": "build_vs_buy", "me": 0,
    })
    assert project.items.count() == 2
    extra = project.items.exclude(type_id=587).first()
    client.post(f"/industry/plans/{project.pk}/items/{extra.id}/remove/")
    assert project.items.count() == 1

    # Status change.
    client.post(f"/industry/plans/{project.pk}/status/", {"status": "done"})
    project.refresh_from_db()
    assert project.status == IndustryProject.Status.DONE


@pytest.mark.django_db
def test_type_search_returns_matches(client, django_user_model, sde):
    user, _ = make_member(django_user_model, "bob", 2002)
    client.force_login(user)
    rows = client.get("/industry/type-search/?q=Rifter").json()
    assert any(r["type_id"] == 587 for r in rows)
    # The build picker passes ?buildable=1; the Rifter (587) is buildable.
    buildable = client.get("/industry/type-search/?buildable=1&q=Rifter").json()
    assert any(r["type_id"] == 587 for r in buildable)
    # A non-buildable item (Fusion S ammo has no blueprint in the fixture) is excluded.
    assert all(r["type_id"] != 192 for r in client.get("/industry/type-search/?buildable=1&q=Fusion").json())


@pytest.mark.django_db
def test_claim_project_and_manage_permission(client, django_user_model, sde):
    owner, _ = make_member(django_user_model, "owner", 2003)
    other, _ = make_member(django_user_model, "other", 2004)
    project = IndustryProject.objects.create(name="Orphan", status=IndustryProject.Status.ACTIVE)

    client.force_login(other)
    client.post(f"/industry/plans/{project.pk}/claim/")
    project.refresh_from_db()
    assert project.assigned_to_id == other.id
    # The non-lead owner cannot change status (not creator/assignee/officer).
    client.force_login(owner)
    assert client.post(f"/industry/plans/{project.pk}/status/", {"status": "blocked"}).status_code == 403
    # Nor can a member yank the lead from the current lead.
    assert client.post(f"/industry/plans/{project.pk}/claim/").status_code == 403
    project.refresh_from_db()
    assert project.assigned_to_id == other.id


@pytest.mark.django_db
def test_industry_create_requires_member(client, django_user_model):
    assert client.get("/industry/plans/new/").status_code == 302  # anon → login
    plain = django_user_model.objects.create(username="plain")
    client.force_login(plain)
    resp = client.get("/industry/plans/new/")  # logged in, not in corp → recruitment
    assert resp.status_code == 302 and resp.headers["Location"] == "/onboarding/"


# --- Logistics ----------------------------------------------------------------
@pytest.mark.django_db
def test_haul_lifecycle(client, django_user_model, sde):
    hauler, _ = make_member(django_user_model, "hauler", 2010)
    jita = MarketLocation.objects.create(name="Jita", region_id=10000002, system_id=30000142)
    amarr = MarketLocation.objects.create(name="Amarr", region_id=10000043, system_id=30002187)
    client.force_login(hauler)

    client.post("/stockpile/logistics/post/", {
        "type_id": 34, "quantity": 1000, "source_location": jita.id, "dest_location": amarr.id,
    })
    task = HaulingTask.objects.get(type_id=34)
    assert task.status == HaulingTask.Status.OPEN and task.volume_m3 > 0

    client.post(f"/stockpile/logistics/{task.pk}/claim/")
    task.refresh_from_db()
    assert task.status == HaulingTask.Status.CLAIMED and task.claimed_by_character_id == 2010

    client.post(f"/stockpile/logistics/{task.pk}/transition/", {"action": "start"})
    task.refresh_from_db()
    assert task.status == HaulingTask.Status.IN_PROGRESS

    client.post(f"/stockpile/logistics/{task.pk}/transition/", {"action": "done"})
    task.refresh_from_db()
    assert task.status == HaulingTask.Status.DONE


@pytest.mark.django_db
def test_haul_transition_blocked_for_non_owner(client, django_user_model, sde):
    a, _ = make_member(django_user_model, "claimer", 2011)
    b, _ = make_member(django_user_model, "stranger", 2012)
    task = HaulingTask.objects.create(
        type_id=34, status=HaulingTask.Status.CLAIMED, claimed_by_character_id=2011
    )
    client.force_login(b)
    assert client.post(f"/stockpile/logistics/{task.pk}/transition/", {"action": "done"}).status_code == 403


# --- Stockpile ----------------------------------------------------------------
@pytest.mark.django_db
def test_member_records_stock_officer_creates_pile(client, django_user_model, sde):
    member, _ = make_member(django_user_model, "stocker", 2020)
    pile = Stockpile.objects.create(name="Hangar")
    client.force_login(member)
    client.post("/stockpile/stock/record/", {
        "stockpile": pile.id, "type_id": 34, "quantity_current": 5000, "quantity_target": 10000,
    })
    item = pile.items.get(type_id=34)
    assert item.quantity_current == 5000 and item.quantity_target == 10000

    # Members cannot create a stockpile; officers can.
    assert client.post("/stockpile/stockpiles/create/", {"name": "X", "kind": "corp"}).status_code == 403
    officer, _ = make_member(django_user_model, "boss", 2021, role=rbac.ROLE_OFFICER)
    client.force_login(officer)
    client.post("/stockpile/stockpiles/create/", {"name": "New Pile", "kind": "corp"})
    assert Stockpile.objects.filter(name="New Pile").exists()


# --- Skill plans --------------------------------------------------------------
def _rifter_doctrine():
    doctrine = Doctrine.objects.create(name="Rifter Roam", status=Doctrine.Status.ACTIVE, priority=50)
    fit = DoctrineFit.objects.create(
        doctrine=doctrine, name="Rifter", ship_type_id=587,
        modules=[{"type_id": 484}, {"type_id": 2046}],
    )
    derive_skill_requirements(fit)
    return doctrine


@pytest.mark.django_db
def test_skill_plan_generate_track_export(client, django_user_model, sde):
    user, char = make_member(django_user_model, "trainee", 2030)
    CharacterSkillSnapshot.objects.create(character=char, skills={}, total_sp=0, is_latest=True)
    doctrine = _rifter_doctrine()
    client.force_login(user)

    resp = client.post("/skills/create/", {"character_id": char.character_id, "doctrine_id": doctrine.id})
    assert resp.status_code == 302
    plan = SkillPlan.objects.get(character=char)
    assert plan.steps.count() == 3  # Minmatar Frigate, Small Projectile Turret, Gunnery
    assert plan.estimated_total_seconds > 0

    # Mark a step trained, then export EVE text.
    step = plan.steps.first()
    client.post(f"/skills/{plan.pk}/steps/{step.id}/toggle/")
    step.refresh_from_db()
    assert step.status == SkillPlanStep.Status.DONE

    text = client.get(f"/skills/{plan.pk}/export/").content.decode()
    assert "Gunnery 1" in text or "Minmatar Frigate 1" in text


@pytest.mark.django_db
def test_skill_plan_is_private_to_owner(client, django_user_model, sde):
    a, char_a = make_member(django_user_model, "ownera", 2031)
    b, _ = make_member(django_user_model, "snoop", 2032)
    CharacterSkillSnapshot.objects.create(character=char_a, skills={}, total_sp=0, is_latest=True)
    doctrine = _rifter_doctrine()
    from apps.skills.services import generate_plan_for_doctrine

    plan = generate_plan_for_doctrine(char_a, doctrine)
    client.force_login(b)
    assert client.get(f"/skills/{plan.pk}/").status_code == 404
    assert client.get(f"/skills/{plan.pk}/export/").status_code == 404
