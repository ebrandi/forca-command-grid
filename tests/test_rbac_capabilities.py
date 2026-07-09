"""4.16 — Least-privilege role model (recruiter / FC capabilities).

Acceptance: a lateral role grants ONE capability without officer-wide authority; officers
keep every capability via a rank baseline; expired grants confer nothing; the capability
gates recruitment (recruiter) and fleet ops (FC) without leaking other officer surfaces.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import AnonymousUser
from django.urls import reverse
from django.utils import timezone

from apps.identity.models import Permission, Role, RoleAssignment
from core import rbac

pytestmark = pytest.mark.django_db


def _user(django_user_model, name):
    return django_user_model.objects.create(username=name)


def _assign(user, role_key, *, perms=(), expires_at=None):
    role, _ = Role.objects.get_or_create(key=role_key)
    for pk in perms:
        role.permissions.add(Permission.objects.get_or_create(key=pk)[0])
    return RoleAssignment.objects.create(user=user, role=role, expires_at=expires_at)


def _reload(user):
    return type(user).objects.get(pk=user.pk)  # drop the request-scoped rank/perm memo


# --- has_perm unit behaviour -------------------------------------------------
def test_officer_holds_every_capability_via_baseline(django_user_model):
    u = _user(django_user_model, "off")
    _assign(u, rbac.ROLE_OFFICER)
    u = _reload(u)
    assert rbac.has_perm(u, rbac.PERM_RECRUITMENT_MANAGE)
    assert rbac.has_perm(u, rbac.PERM_FLEET_MANAGE)


def test_plain_member_holds_no_capability(django_user_model):
    u = _user(django_user_model, "mem")
    _assign(u, rbac.ROLE_MEMBER)
    u = _reload(u)
    assert not rbac.has_perm(u, rbac.PERM_RECRUITMENT_MANAGE)
    assert not rbac.has_perm(u, rbac.PERM_FLEET_MANAGE)


def test_recruiter_has_only_recruitment_and_no_extra_rank(django_user_model):
    u = _user(django_user_model, "rec")
    _assign(u, rbac.ROLE_MEMBER)
    _assign(u, rbac.ROLE_RECRUITER, perms=[rbac.PERM_RECRUITMENT_MANAGE])
    u = _reload(u)
    assert rbac.has_perm(u, rbac.PERM_RECRUITMENT_MANAGE)
    assert not rbac.has_perm(u, rbac.PERM_FLEET_MANAGE)  # least privilege
    assert rbac.effective_rank(u) == rbac.ROLE_RANK[rbac.ROLE_MEMBER]  # NOT promoted to officer
    assert not rbac.has_role(u, rbac.ROLE_OFFICER)


def test_fc_has_only_fleet(django_user_model):
    u = _user(django_user_model, "fc")
    _assign(u, rbac.ROLE_MEMBER)
    _assign(u, rbac.ROLE_FC, perms=[rbac.PERM_FLEET_MANAGE])
    u = _reload(u)
    assert rbac.has_perm(u, rbac.PERM_FLEET_MANAGE)
    assert not rbac.has_perm(u, rbac.PERM_RECRUITMENT_MANAGE)


def test_expired_grant_confers_nothing(django_user_model):
    u = _user(django_user_model, "exp")
    _assign(u, rbac.ROLE_MEMBER)
    _assign(u, rbac.ROLE_RECRUITER, perms=[rbac.PERM_RECRUITMENT_MANAGE],
            expires_at=timezone.now() - dt.timedelta(hours=1))
    u = _reload(u)
    assert not rbac.has_perm(u, rbac.PERM_RECRUITMENT_MANAGE)


def test_superuser_holds_all_and_anon_holds_none(django_user_model):
    su = django_user_model.objects.create(username="su", is_superuser=True)
    assert rbac.has_perm(su, rbac.PERM_FLEET_MANAGE)
    assert not rbac.has_perm(AnonymousUser(), rbac.PERM_FLEET_MANAGE)


def test_unknown_capability_fails_closed(django_user_model):
    u = _user(django_user_model, "u2")
    _assign(u, rbac.ROLE_OFFICER)
    u = _reload(u)
    # Unknown key → DIRECTOR baseline → an officer does NOT hold it (typo never grants).
    assert not rbac.has_perm(u, "made.up.permission")


# --- decorator / view integration --------------------------------------------
def test_recruiter_reaches_recruitment_member_does_not(client, django_user_model):
    member = _user(django_user_model, "m1")
    _assign(member, rbac.ROLE_MEMBER)
    client.force_login(member)
    assert client.get(reverse("recruitment:list")).status_code == 403  # no capability

    recruiter = _user(django_user_model, "r1")
    _assign(recruiter, rbac.ROLE_MEMBER)
    _assign(recruiter, rbac.ROLE_RECRUITER, perms=[rbac.PERM_RECRUITMENT_MANAGE])
    client.force_login(recruiter)
    assert client.get(reverse("recruitment:list")).status_code == 200


def test_recruiter_cannot_reach_officer_only_surface(client, django_user_model):
    # Least privilege: a recruiter is not an officer, so the SRP queue stays forbidden.
    recruiter = _user(django_user_model, "r2")
    _assign(recruiter, rbac.ROLE_MEMBER)
    _assign(recruiter, rbac.ROLE_RECRUITER, perms=[rbac.PERM_RECRUITMENT_MANAGE])
    client.force_login(recruiter)
    assert client.get(reverse("srp:queue")).status_code == 403
    assert client.get(reverse("operations:create")).status_code == 403  # not an FC either


def test_fc_reaches_op_create_but_not_recruitment(client, django_user_model):
    fc = _user(django_user_model, "f1")
    _assign(fc, rbac.ROLE_MEMBER)
    _assign(fc, rbac.ROLE_FC, perms=[rbac.PERM_FLEET_MANAGE])
    client.force_login(fc)
    assert client.get(reverse("operations:create")).status_code == 200
    assert client.get(reverse("recruitment:list")).status_code == 403
