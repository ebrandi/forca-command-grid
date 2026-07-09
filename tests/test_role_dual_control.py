"""4.17 — Role-change dual-control + expiring grants.

Acceptance: granting a dual-control role (director) opens a request a SECOND director must
approve (requester can't self-approve); non-dual-control roles apply immediately; a grant
can carry an expiry; the last-director revoke guard is intact.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.identity.models import RoleAssignment, RoleChangeRequest
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db


def _director(django_user_model, name):
    u = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_DIRECTOR))
    return u


def _set_role_url(uid):
    return reverse("admin_audit:set_role", args=[uid])


def test_grant_officer_applies_immediately(client, django_user_model):
    d = _director(django_user_model, "d1")
    target = django_user_model.objects.create(username="t1")
    client.force_login(d)
    client.post(_set_role_url(target.id), {"grant": "officer"})
    assert RoleAssignment.objects.filter(user=target, role__key="officer").exists()
    assert not RoleChangeRequest.objects.exists()  # no dual-control for officer


def test_grant_director_opens_request_not_applied(client, django_user_model):
    d = _director(django_user_model, "d1")
    target = django_user_model.objects.create(username="t2")
    client.force_login(d)
    client.post(_set_role_url(target.id), {"grant": "director", "reason": "promote"})
    assert not RoleAssignment.objects.filter(user=target, role__key="director").exists()
    req = RoleChangeRequest.objects.get(target=target, role_key="director")
    assert req.status == RoleChangeRequest.Status.PENDING and req.requested_by_id == d.id


def test_requester_cannot_approve_own(client, django_user_model):
    d = _director(django_user_model, "d1")
    target = django_user_model.objects.create(username="t3")
    client.force_login(d)
    client.post(_set_role_url(target.id), {"grant": "director"})
    req = RoleChangeRequest.objects.get()
    client.post(reverse("admin_audit:role_request_decide", args=[req.id]), {"decision": "approve"})
    req.refresh_from_db()
    assert req.status == RoleChangeRequest.Status.PENDING  # blocked — SoD
    assert not RoleAssignment.objects.filter(user=target, role__key="director").exists()


def test_second_director_approves_and_applies(client, django_user_model):
    d1 = _director(django_user_model, "d1")
    d2 = _director(django_user_model, "d2")
    target = django_user_model.objects.create(username="t4")
    client.force_login(d1)
    client.post(_set_role_url(target.id), {"grant": "director"})
    req = RoleChangeRequest.objects.get()
    client.force_login(d2)
    client.post(reverse("admin_audit:role_request_decide", args=[req.id]), {"decision": "approve"})
    req.refresh_from_db()
    assert req.status == RoleChangeRequest.Status.APPROVED and req.decided_by_id == d2.id
    assert RoleAssignment.objects.filter(user=target, role__key="director").exists()


def test_reject_leaves_no_grant(client, django_user_model):
    d1 = _director(django_user_model, "d1")
    d2 = _director(django_user_model, "d2")
    target = django_user_model.objects.create(username="t5")
    client.force_login(d1)
    client.post(_set_role_url(target.id), {"grant": "director"})
    req = RoleChangeRequest.objects.get()
    client.force_login(d2)
    client.post(reverse("admin_audit:role_request_decide", args=[req.id]), {"decision": "reject"})
    req.refresh_from_db()
    assert req.status == RoleChangeRequest.Status.REJECTED
    assert not RoleAssignment.objects.filter(user=target, role__key="director").exists()


def test_grant_carries_expiry(client, django_user_model):
    d = _director(django_user_model, "d1")
    target = django_user_model.objects.create(username="t6")
    client.force_login(d)
    future = (timezone.now() + dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M")
    client.post(_set_role_url(target.id), {"grant": "fc", "expires_at": future})
    ra = RoleAssignment.objects.get(user=target, role__key="fc")
    assert ra.expires_at is not None and ra.expires_at > timezone.now()


def test_duplicate_pending_request_blocked(client, django_user_model):
    d = _director(django_user_model, "d1")
    target = django_user_model.objects.create(username="t7")
    client.force_login(d)
    client.post(_set_role_url(target.id), {"grant": "director"})
    client.post(_set_role_url(target.id), {"grant": "director"})
    assert RoleChangeRequest.objects.filter(target=target, status="pending").count() == 1


def test_last_director_floor_ignores_expired_grants(client, django_user_model):
    # Review MED: an EXPIRED director row must not inflate the floor and let the last
    # *effective* director be removed.
    keeper = _director(django_user_model, "keeper")
    stale = django_user_model.objects.create(username="stale")
    RoleAssignment.objects.create(user=stale, role=ensure_role(rbac.ROLE_DIRECTOR),
                                  expires_at=timezone.now() - dt.timedelta(hours=1))
    client.force_login(keeper)
    client.post(_set_role_url(keeper.id), {"revoke": "director"})  # try to drop the last active one
    assert keeper.role_assignments.filter(role__key="director").exists()  # blocked — still director


def test_past_expiry_is_rejected_not_permanent(client, django_user_model):
    d = _director(django_user_model, "d1")
    target = django_user_model.objects.create(username="t9")
    client.force_login(d)
    past = (timezone.now() - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    client.post(_set_role_url(target.id), {"grant": "fc", "expires_at": past})
    assert not RoleAssignment.objects.filter(user=target, role__key="fc").exists()  # not granted


def test_non_director_cannot_reach_set_role(client, django_user_model):
    member = django_user_model.objects.create(username="m1")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    target = django_user_model.objects.create(username="t8")
    client.force_login(member)
    assert client.post(_set_role_url(target.id), {"grant": "officer"}).status_code == 403
