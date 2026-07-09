"""Gap B4/B5 — admin console pages for fleet-support skills and the staging system."""
from __future__ import annotations

import pytest

from apps.identity.models import RoleAssignment
from apps.readiness.models import FleetSupportSkill, StagingSystem
from apps.sde.models import SdeCategory, SdeGroup, SdeRegion, SdeSolarSystem, SdeType
from apps.sso.services import ensure_role
from core import rbac

ARMORED_CMD = 20494


def _director(django_user_model, name="dir"):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_DIRECTOR))
    return user


def _member(django_user_model, name="m"):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    return user


def _skill_type(type_id=ARMORED_CMD, name="Armored Command"):
    cat, _ = SdeCategory.objects.get_or_create(category_id=16, defaults={"name": "Skill"})
    grp, _ = SdeGroup.objects.get_or_create(group_id=257, defaults={"category": cat, "name": "Command"})
    return SdeType.objects.get_or_create(
        type_id=type_id, defaults={"name": name, "group": grp, "published": True})[0]


def _system(system_id=30000142, name="Jita"):
    region, _ = SdeRegion.objects.get_or_create(region_id=10000002, defaults={"name": "The Forge"})
    return SdeSolarSystem.objects.get_or_create(
        system_id=system_id, defaults={"region": region, "name": name})[0]


# --- gating ------------------------------------------------------------------
@pytest.mark.django_db
def test_pages_are_director_only(client, django_user_model):
    client.force_login(_member(django_user_model))
    assert client.get("/ops/admin/readiness/support/").status_code == 403
    assert client.get("/ops/admin/readiness/staging/").status_code == 403


# --- B4: fleet-support skills CRUD + search ----------------------------------
@pytest.mark.django_db
def test_support_skill_create_update_delete(client, django_user_model):
    from apps.admin_audit.models import AuditLog

    client.force_login(_director(django_user_model))
    _skill_type()
    client.post("/ops/admin/readiness/support/create/",
                {"skill_type_id": str(ARMORED_CMD), "min_level": "4", "active": "on"})
    sk = FleetSupportSkill.objects.get()
    assert sk.skill_type_id == ARMORED_CMD and sk.min_level == 4 and sk.skill_name == "Armored Command"
    assert AuditLog.objects.filter(action="readiness.support_skill.create").exists()
    client.post(f"/ops/admin/readiness/support/{sk.id}/update/",
                {"skill_type_id": str(ARMORED_CMD), "min_level": "5", "active": "on"})
    sk.refresh_from_db()
    assert sk.min_level == 5
    client.post(f"/ops/admin/readiness/support/{sk.id}/delete/")
    assert not FleetSupportSkill.objects.exists()


@pytest.mark.django_db
def test_support_skill_rejects_unknown_and_duplicate(client, django_user_model):
    client.force_login(_director(django_user_model, "dir2"))
    # Unknown type id (not in SDE) is rejected.
    client.post("/ops/admin/readiness/support/create/", {"skill_type_id": "999999", "min_level": "5"})
    assert not FleetSupportSkill.objects.exists()
    # Duplicate is rejected.
    _skill_type()
    FleetSupportSkill.objects.create(skill_type_id=ARMORED_CMD, skill_name="Armored Command", min_level=3)
    client.post("/ops/admin/readiness/support/create/", {"skill_type_id": str(ARMORED_CMD), "min_level": "5"})
    assert FleetSupportSkill.objects.count() == 1


@pytest.mark.django_db
def test_support_skill_search_returns_skills(client, django_user_model):
    client.force_login(_director(django_user_model, "dir3"))
    _skill_type()
    rows = client.get("/ops/admin/readiness/support/search/?q=Armored").json()
    assert any(r["type_id"] == ARMORED_CMD for r in rows)


@pytest.mark.django_db
def test_support_page_renders(client, django_user_model):
    client.force_login(_director(django_user_model, "dir4"))
    FleetSupportSkill.objects.create(skill_type_id=ARMORED_CMD, skill_name="Armored Command", min_level=4)
    html = client.get("/ops/admin/readiness/support/").content.decode()
    assert "Fleet support skills" in html and "Armored Command" in html


# --- B5: staging system set / clear ------------------------------------------
@pytest.mark.django_db
def test_staging_set_and_clear(client, django_user_model):
    from apps.admin_audit.models import AuditLog

    client.force_login(_director(django_user_model, "dir5"))
    _system()
    client.post("/ops/admin/readiness/staging/", {"system_id": "30000142"})
    st = StagingSystem.objects.get()
    assert st.system_id == 30000142 and st.system_name == "Jita" and st.active
    assert AuditLog.objects.filter(action="readiness.staging.set").exists()
    # Setting a new one replaces the old (single active staging).
    _system(30002187, "Amarr")
    client.post("/ops/admin/readiness/staging/", {"system_id": "30002187"})
    assert StagingSystem.objects.count() == 1 and StagingSystem.objects.get().system_id == 30002187
    # Clear.
    client.post("/ops/admin/readiness/staging/clear/")
    assert not StagingSystem.objects.exists()


@pytest.mark.django_db
def test_staging_rejects_unknown_system(client, django_user_model):
    client.force_login(_director(django_user_model, "dir6"))
    client.post("/ops/admin/readiness/staging/", {"system_id": "999999"})
    assert not StagingSystem.objects.exists()


@pytest.mark.django_db
def test_staging_page_renders(client, django_user_model):
    client.force_login(_director(django_user_model, "dir7"))
    StagingSystem.objects.create(system_id=30000142, system_name="Jita", active=True)
    html = client.get("/ops/admin/readiness/staging/").content.decode()
    assert "Staging system" in html and "Jita" in html
