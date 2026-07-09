"""Console: add/remove 'also bring' recommendations (DoctrineRequirement) on a fit."""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.doctrines.models import Doctrine, DoctrineFit, DoctrineRequirement
from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac


def _user(django_user_model, uid, role=rbac.ROLE_OFFICER):
    u = django_user_model.objects.create(username=f"dr-{uid}")
    RoleAssignment.objects.create(user=u, role=ensure_role(role))
    return u


def _fit():
    doc = Doctrine.objects.create(name="Ferox Fleet")
    return DoctrineFit.objects.create(doctrine=doc, name="Railgun Ferox", ship_type_id=16227)


@pytest.mark.django_db
def test_officer_adds_and_removes_requirement(client, django_user_model):
    fit = _fit()
    client.force_login(_user(django_user_model, 1))
    resp = client.post(reverse("admin_audit:requirement_add", args=[fit.id]),
                       {"kind": "implant", "type_id": "33077", "is_recommended": "on"})
    assert resp.status_code == 302
    req = DoctrineRequirement.objects.get(fit=fit)
    assert req.kind == "implant" and req.type_id == 33077 and req.is_recommended is True

    client.post(reverse("admin_audit:requirement_delete", args=[req.id]))
    assert not DoctrineRequirement.objects.filter(pk=req.id).exists()


@pytest.mark.django_db
def test_note_requirement_needs_text(client, django_user_model):
    fit = _fit()
    client.force_login(_user(django_user_model, 2))
    client.post(reverse("admin_audit:requirement_add", args=[fit.id]), {"kind": "note", "text": ""})
    assert DoctrineRequirement.objects.count() == 0
    client.post(reverse("admin_audit:requirement_add", args=[fit.id]),
                {"kind": "note", "text": "Bring a mobile depot"})
    assert DoctrineRequirement.objects.filter(kind="note", text="Bring a mobile depot").exists()


@pytest.mark.django_db
def test_non_note_requirement_needs_type_id(client, django_user_model):
    fit = _fit()
    client.force_login(_user(django_user_model, 3))
    client.post(reverse("admin_audit:requirement_add", args=[fit.id]), {"kind": "rig", "type_id": ""})
    assert DoctrineRequirement.objects.count() == 0


@pytest.mark.django_db
def test_oversized_type_id_is_rejected(client, django_user_model):
    """A type_id past the 32-bit IntegerField range is treated as absent (would 500 the
    insert on Postgres otherwise) — a non-note requirement is then rejected."""
    fit = _fit()
    client.force_login(_user(django_user_model, 5))
    client.post(reverse("admin_audit:requirement_add", args=[fit.id]),
                {"kind": "rig", "type_id": "99999999999"})
    assert DoctrineRequirement.objects.count() == 0


@pytest.mark.django_db
def test_member_cannot_add_requirement(client, django_user_model):
    fit = _fit()
    client.force_login(_user(django_user_model, 4, rbac.ROLE_MEMBER))
    assert client.post(reverse("admin_audit:requirement_add", args=[fit.id]),
                       {"kind": "note", "text": "x"}).status_code == 403
