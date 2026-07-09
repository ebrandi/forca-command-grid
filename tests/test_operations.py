"""Operations planner: readiness scoring, pilot prep, task generation."""
from __future__ import annotations

import pytest

from apps.characters.models import CharacterSkillSnapshot
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit, SkillRequirement
from apps.identity.models import RoleAssignment
from apps.operations import services
from apps.operations.models import Operation, OperationDoctrine
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from apps.tasks.models import Task
from core import rbac

GUNNERY = 3300
RIFTER = 587


def _doctrine(name, level):
    cat, _ = DoctrineCategory.objects.get_or_create(key="dps", label="DPS")
    d = Doctrine.objects.create(name=name, category=cat, priority=80)
    fit = DoctrineFit.objects.create(doctrine=d, name=name, ship_type_id=RIFTER)
    SkillRequirement.objects.create(fit=fit, skill_type_id=GUNNERY, min_level=level, optimal_level=level)
    return d


def _char(django_user_model, cid, level):
    user = django_user_model.objects.create(username=f"eve:{cid}")
    ch = EveCharacter.objects.create(character_id=cid, user=user, name=f"P{cid}", is_main=True, is_corp_member=True)
    CharacterSkillSnapshot.objects.create(
        character=ch, is_latest=True, skills={str(GUNNERY): {"trained_level": level, "sp": 0}}
    )
    return user, ch


@pytest.mark.django_db
def test_operation_readiness_and_pilot_prep(django_user_model, sde):
    d = _doctrine("Core", 3)
    _char(django_user_model, 8001, 5)  # can fly
    _, can = _char(django_user_model, 8002, 1)  # cannot

    op = Operation.objects.create(name="Home Defence", type=Operation.Type.HOME_DEFENCE)
    OperationDoctrine.objects.create(operation=op, doctrine=d)

    r = services.operation_readiness(op)
    assert r["rows"][0]["ready"] == 1 and r["rows"][0]["known"] == 2
    assert r["pct"] == 50
    assert any(g["doctrine_id"] == d.id for g in r["gaps"])

    prep = services.pilot_readiness(op, can)
    assert prep[0]["ready"] is False  # the low-skill pilot isn't ready


@pytest.mark.django_db
def test_generate_tasks_idempotent_and_officer_only(client, django_user_model, sde):
    d = _doctrine("Core", 3)
    _char(django_user_model, 8003, 1)  # not ready → a gap exists
    op = Operation.objects.create(name="Deploy", type=Operation.Type.DEPLOYMENT)
    OperationDoctrine.objects.create(operation=op, doctrine=d)

    officer = django_user_model.objects.create(username="fc")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    client.force_login(officer)
    client.post(f"/operations/{op.pk}/tasks/")
    client.post(f"/operations/{op.pk}/tasks/")  # idempotent
    assert Task.objects.filter(related_type="operation", related_id=f"{op.pk}:{d.id}").count() == 1


@pytest.mark.django_db
def test_member_cannot_create_operation(client, django_user_model, sde):
    member = django_user_model.objects.create(username="m")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    assert client.post("/operations/create/", {"name": "x"}).status_code == 403
    assert client.get("/operations/").status_code == 200  # but can view
