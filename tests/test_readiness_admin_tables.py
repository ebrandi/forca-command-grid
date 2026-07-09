"""Phase 6 UI — admin CRUD for MandatoryShip & StrategicRoleTarget (Director-gated)."""
from __future__ import annotations

import pytest

from apps.identity.models import RoleAssignment
from apps.readiness.models import MandatoryShip, StrategicRoleTarget
from apps.sso.services import ensure_role
from core import rbac


def _director(django_user_model, name="dir"):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_DIRECTOR))
    return user


def _member(django_user_model, name="m"):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_OFFICER))  # officer, not director
    return user


# --- gating ------------------------------------------------------------------
@pytest.mark.django_db
def test_pages_are_director_only(client, django_user_model):
    client.force_login(_member(django_user_model))
    assert client.get("/ops/admin/readiness/mandatory-ships/").status_code == 403
    assert client.get("/ops/admin/readiness/strategic-roles/").status_code == 403


# --- mandatory ships CRUD ----------------------------------------------------
@pytest.mark.django_db
def test_mandatory_ship_create_update_delete(client, django_user_model):
    from apps.admin_audit.models import AuditLog

    client.force_login(_director(django_user_model))
    # Create.
    client.post("/ops/admin/readiness/mandatory-ships/create/", {
        "label": "Travel Interceptor", "category": "travel", "ship_type_id": "11176",
        "required_quantity": "1", "active": "on",
    })
    ship = MandatoryShip.objects.get()
    assert ship.label == "Travel Interceptor" and ship.ship_type_id == 11176
    assert AuditLog.objects.filter(action="readiness.mandatory_ship.create").exists()
    # Update.
    client.post(f"/ops/admin/readiness/mandatory-ships/{ship.id}/update/", {
        "label": "Ceptor", "category": "travel", "ship_type_id": "11176",
        "required_quantity": "2", "active": "on",
    })
    ship.refresh_from_db()
    assert ship.label == "Ceptor" and ship.required_quantity == 2
    # Delete.
    client.post(f"/ops/admin/readiness/mandatory-ships/{ship.id}/delete/")
    assert not MandatoryShip.objects.exists()


@pytest.mark.django_db
def test_mandatory_ship_requires_label_and_type(client, django_user_model):
    client.force_login(_director(django_user_model, "dir2"))
    client.post("/ops/admin/readiness/mandatory-ships/create/", {"label": "", "ship_type_id": "11176"})
    client.post("/ops/admin/readiness/mandatory-ships/create/", {"label": "X", "ship_type_id": ""})
    assert not MandatoryShip.objects.exists()  # both rejected


# --- strategic roles CRUD ----------------------------------------------------
@pytest.mark.django_db
def test_strategic_role_create_with_json_params(client, django_user_model):
    client.force_login(_director(django_user_model, "dir3"))
    client.post("/ops/admin/readiness/strategic-roles/create/", {
        "role_key": "logi", "label": "Logistics", "desired_count": "12",
        "detection": "skills", "detection_params": '{"skills": {"3300": 5}}', "active": "on",
    })
    role = StrategicRoleTarget.objects.get()
    assert role.role_key == "logi" and role.desired_count == 12
    assert role.detection_params == {"skills": {"3300": 5}}


@pytest.mark.django_db
def test_strategic_role_rejects_bad_json_and_unknown_key(client, django_user_model):
    client.force_login(_director(django_user_model, "dir4"))
    # Bad JSON.
    client.post("/ops/admin/readiness/strategic-roles/create/", {
        "role_key": "logi", "detection": "skills", "detection_params": "{not json",
    })
    assert not StrategicRoleTarget.objects.exists()
    # Unknown role key (not in catalogue).
    client.post("/ops/admin/readiness/strategic-roles/create/", {
        "role_key": "wizard", "detection": "manual", "detection_params": "",
    })
    assert not StrategicRoleTarget.objects.exists()


@pytest.mark.django_db
def test_strategic_role_no_duplicate_key(client, django_user_model):
    client.force_login(_director(django_user_model, "dir5"))
    StrategicRoleTarget.objects.create(role_key="fc", label="FC", desired_count=3)
    client.post("/ops/admin/readiness/strategic-roles/create/", {
        "role_key": "fc", "detection": "manual", "detection_params": "",
    })
    assert StrategicRoleTarget.objects.filter(role_key="fc").count() == 1


@pytest.mark.django_db
def test_pages_render_for_director(client, django_user_model):
    client.force_login(_director(django_user_model, "dir6"))
    MandatoryShip.objects.create(label="Ceptor", ship_type_id=11176)
    StrategicRoleTarget.objects.create(role_key="logi", label="Logistics", desired_count=12,
                                       detection="skills", detection_params={"skills": {"3300": 5}})
    ms = client.get("/ops/admin/readiness/mandatory-ships/").content.decode()
    sr = client.get("/ops/admin/readiness/strategic-roles/").content.decode()
    assert "Mandatory ships" in ms and "Ceptor" in ms
    assert "Strategic roles" in sr and "logi" in sr
    assert '{"skills": {"3300": 5}}' in sr  # params serialised as JSON for the edit field
