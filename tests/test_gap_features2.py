"""Second batch of member-facing PRD gaps: doctrine non-skill requirements,
skill-plan editing, and the 'best next doctrine to unlock' ranking."""
from __future__ import annotations

import pytest

from apps.characters.models import CharacterSkillSnapshot
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit, DoctrineRequirement
from apps.doctrines.services import derive_skill_requirements
from apps.identity.models import RoleAssignment
from apps.skills.models import SkillPlan
from apps.skills.services import generate_plan_for_doctrine
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac


def _member(django_user_model, name="m"):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    return user


def _rifter_doctrine():
    cat = DoctrineCategory.objects.create(key="dps", label="DPS")
    doc = Doctrine.objects.create(name="Rifter Roam", category=cat, status=Doctrine.Status.ACTIVE)
    fit = DoctrineFit.objects.create(
        doctrine=doc, name="Rifter", ship_type_id=587,
        modules=[{"type_id": 484}, {"type_id": 2046}],
    )
    derive_skill_requirements(fit)
    return doc, fit


# --- DOC-10: non-skill requirements shown on the fit ---------------------------
@pytest.mark.django_db
def test_doctrine_non_skill_requirements_shown(client, django_user_model, sde):
    doc, fit = _rifter_doctrine()
    DoctrineRequirement.objects.create(
        fit=fit, kind=DoctrineRequirement.Kind.AMMO, type_id=192, is_recommended=False
    )
    DoctrineRequirement.objects.create(
        fit=fit, kind=DoctrineRequirement.Kind.NOTE, text="Bring a warp core stabiliser", is_recommended=True
    )
    client.force_login(_member(django_user_model))
    html = client.get(f"/doctrines/{doc.pk}/").content.decode()
    assert "Also bring" in html
    assert "Fusion S" in html  # ammo type 192 resolved
    assert "warp core stabiliser" in html


# --- SKL-10: edit/reorder a skill plan -----------------------------------------
@pytest.mark.django_db
def test_skill_plan_remove_and_move_step(client, django_user_model, sde):
    user = _member(django_user_model)
    char = EveCharacter.objects.create(character_id=1, user=user, name="P", is_main=True,
                                       is_corp_member=True)
    CharacterSkillSnapshot.objects.create(character=char, skills={}, total_sp=0, is_latest=True)
    doc, _ = _rifter_doctrine()
    plan = generate_plan_for_doctrine(char, doc)
    steps = list(plan.steps.all())
    assert len(steps) >= 3
    first, second = steps[0], steps[1]

    client.force_login(user)
    # Move the second step up -> it becomes first.
    client.post(f"/skills/{plan.pk}/steps/{second.id}/move/", {"dir": "up"})
    assert list(plan.steps.all())[0].id == second.id

    # Remove a step -> count drops and orders re-sequence from 0.
    before = plan.steps.count()
    client.post(f"/skills/{plan.pk}/steps/{first.id}/remove/")
    assert plan.steps.count() == before - 1
    assert list(plan.steps.values_list("order", flat=True)) == list(range(before - 1))


@pytest.mark.django_db
def test_skill_plan_edits_are_owner_only(client, django_user_model, sde):
    a = _member(django_user_model, "a")
    b = _member(django_user_model, "b")
    char = EveCharacter.objects.create(character_id=2, user=a, name="A", is_main=True,
                                       is_corp_member=True)
    CharacterSkillSnapshot.objects.create(character=char, skills={}, total_sp=0, is_latest=True)
    doc, _ = _rifter_doctrine()
    plan = generate_plan_for_doctrine(char, doc)
    step = plan.steps.first()
    client.force_login(b)
    assert client.post(f"/skills/{plan.pk}/steps/{step.id}/remove/").status_code == 404


# --- DOC-08 / SKL-07: best next doctrine to unlock -----------------------------
@pytest.mark.django_db
def test_my_readiness_ranks_and_builds_plan(client, django_user_model, sde):
    user = _member(django_user_model)
    char = EveCharacter.objects.create(character_id=3, user=user, name="P", is_main=True,
                                       is_corp_member=True)
    CharacterSkillSnapshot.objects.create(character=char, skills={}, total_sp=0, is_latest=True)
    doc, _ = _rifter_doctrine()

    client.force_login(user)
    resp = client.get("/doctrines/my-readiness/")
    assert resp.status_code == 200
    # The not-yet-flyable doctrine appears under "closest to unlock".
    assert {r["doctrine_id"] for r in resp.context["near"]} == {doc.pk}
    assert b"Build skill plan" in resp.content

    # The CTA posts to skills:create and produces a plan.
    client.post("/skills/create/", {"character_id": char.character_id, "doctrine_id": doc.pk})
    assert SkillPlan.objects.filter(character=char, target_doctrine=doc).exists()


@pytest.mark.django_db
def test_my_readiness_member_only(client, django_user_model, sde):
    # /doctrines follows the "Ships & doctrines" audience (default corp). A signed-out
    # visitor is sent to log in; a logged-in outsider gets the audience gate's 404.
    assert client.get("/doctrines/my-readiness/").status_code == 302  # anon -> log in
    client.force_login(django_user_model.objects.create(username="outsider"))
    assert client.get("/doctrines/my-readiness/").status_code == 404  # non-member
