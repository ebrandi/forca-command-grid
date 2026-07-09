"""RBAC tests."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied

from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac


def test_anonymous_is_public():
    assert rbac.has_role(AnonymousUser(), rbac.ROLE_PUBLIC) is True
    assert rbac.has_role(AnonymousUser(), rbac.ROLE_MEMBER) is False


@pytest.mark.django_db
def test_member_and_officer_ranks(user):
    assert rbac.has_role(user, rbac.ROLE_MEMBER) is False
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    user.refresh_from_db()
    assert rbac.has_role(user, rbac.ROLE_MEMBER) is True
    assert rbac.has_role(user, rbac.ROLE_OFFICER) is False
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    assert rbac.has_role(user, rbac.ROLE_OFFICER) is True


@pytest.mark.django_db
def test_superuser_is_admin(user):
    user.is_superuser = True
    user.save()
    assert rbac.has_role(user, rbac.ROLE_ADMIN) is True


@pytest.mark.django_db
def test_role_required_decorator(user):
    @rbac.role_required(rbac.ROLE_OFFICER)
    def view(request):
        return "ok"

    request = type("R", (), {"user": user})()
    with pytest.raises(PermissionDenied):
        view(request)
