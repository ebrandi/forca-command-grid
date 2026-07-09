"""Corporation readiness index: dimensions, coverage honesty, gap→task."""
from __future__ import annotations

import pytest

from apps.characters.models import CharacterSkillSnapshot
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit, SkillRequirement
from apps.identity.models import RoleAssignment
from apps.readiness.services import compute_readiness
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from apps.tasks.models import Task
from core import rbac

GUNNERY = 3300
RIFTER = 587


def _doctrine(name, priority, req_level):
    cat, _ = DoctrineCategory.objects.get_or_create(key="dps", label="DPS")
    d = Doctrine.objects.create(name=name, category=cat, priority=priority)
    fit = DoctrineFit.objects.create(doctrine=d, name=name, ship_type_id=RIFTER)
    SkillRequirement.objects.create(fit=fit, skill_type_id=GUNNERY, min_level=req_level, optimal_level=req_level)
    return d


def _char(django_user_model, cid, gunnery_level):
    user = django_user_model.objects.create(username=f"eve:{cid}")
    ch = EveCharacter.objects.create(character_id=cid, user=user, name=f"P{cid}", is_corp_member=True)
    CharacterSkillSnapshot.objects.create(
        character=ch, is_latest=True, skills={str(GUNNERY): {"trained_level": gunnery_level, "sp": 0}}
    )
    return ch


@pytest.mark.django_db
def test_index_reflects_doctrine_coverage(django_user_model, sde):
    _doctrine("Core", 100, 3)
    _char(django_user_model, 7001, 5)  # can fly
    _char(django_user_model, 7002, 1)  # cannot

    result = compute_readiness()
    # 1 of 2 known can fly the only doctrine → doctrine 50, skill 50.
    assert result["dimensions"]["doctrine"] == 50
    assert result["dimensions"]["skill"] == 50
    assert 0 <= result["index"] <= 100
    assert result["coverage"]["known"] == 2
    # The under-covered doctrine appears as a gap with a task suggestion.
    assert any(g["kind"] == "doctrine" for g in result["gaps"])


@pytest.mark.django_db
def test_snapshot_task_persists_history_but_warm_does_not(django_user_model, sde):
    """RDY-1: the dedicated snapshot task writes ReadinessSnapshot history; the warm
    beat (cache/findings only) must not — otherwise the timeline/forecast stay empty."""
    from apps.readiness.models import ReadinessSnapshot
    from apps.readiness.tasks import snapshot_readiness, warm_readiness

    _doctrine("Core", 100, 3)
    _char(django_user_model, 7101, 5)

    assert ReadinessSnapshot.objects.count() == 0
    # warm keeps the read-cache fresh but must NOT persist a history row.
    warm_readiness()
    assert ReadinessSnapshot.objects.count() == 0
    # the dedicated task persists exactly one snapshot and returns the index.
    idx = snapshot_readiness()
    assert isinstance(idx, int)
    assert ReadinessSnapshot.objects.count() == 1
    snap = ReadinessSnapshot.objects.get()
    assert snap.index == idx
    assert "doctrine" in snap.dimensions


@pytest.mark.django_db
def test_unknown_members_excluded_from_denominator(django_user_model, sde):
    _doctrine("Core", 100, 3)
    _char(django_user_model, 7003, 5)  # known + can fly
    # A corp member with no skill import → unknown, must not drag the score down.
    u = django_user_model.objects.create(username="eve:7004")
    EveCharacter.objects.create(character_id=7004, user=u, name="NoImport", is_corp_member=True)

    result = compute_readiness()
    assert result["dimensions"]["doctrine"] == 100  # the one known pilot can fly
    assert result["coverage"]["known"] == 1
    assert result["coverage"]["characters"] == 2


@pytest.mark.django_db
def test_create_task_from_gap_is_idempotent(client, django_user_model, sde):
    officer = django_user_model.objects.create(username="fc")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    client.force_login(officer)

    payload = {"kind": "doctrine", "ref_id": "5", "title": "Train pilots into Core", "task_type": "train"}
    client.post("/readiness/gap/task/", payload)
    client.post("/readiness/gap/task/", payload)  # second time: no duplicate
    assert Task.objects.filter(related_type="gap:doctrine", related_id="5").count() == 1


@pytest.mark.django_db
def test_dashboard_is_officer_only(client, django_user_model, sde):
    member = django_user_model.objects.create(username="m")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    assert client.get("/readiness/").status_code == 403


@pytest.mark.django_db
def test_dashboard_has_quick_rail_and_forecast_banner(client, django_user_model, sde):
    from apps.readiness.models import ReadinessFinding

    officer = django_user_model.objects.create(username="off")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    # An open forecast finding lights the dashboard banner.
    ReadinessFinding.objects.create(
        dimension_key="doctrine", kind=ReadinessFinding.Kind.FORECAST,
        status=ReadinessFinding.Status.OPEN, title="Doctrine trending to red", weight=10,
    )
    client.force_login(officer)
    html = client.get("/readiness/").content.decode()
    assert "/readiness/report/" in html          # quick-rail link
    assert "/readiness/timeline/" in html
    assert "forecast" in html.lower()             # forecast banner
