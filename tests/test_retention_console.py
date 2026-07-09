"""Data-retention console: Director-gated per-DataClass windows + on-leave policy."""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.admin_audit.models import AuditLog, DataRetentionPolicy
from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac


def _user(django_user_model, uid, role):
    u = django_user_model.objects.create(username=f"ret-{uid}")
    RoleAssignment.objects.create(user=u, role=ensure_role(role))
    return u


def _full_post(days="90", leave="retain", active=True):
    post = {}
    for dc, _ in DataRetentionPolicy.DataClass.choices:
        post[f"days:{dc}"] = days
        post[f"on_leave:{dc}"] = leave
        if active:
            post[f"active:{dc}"] = "on"
    return post


@pytest.mark.django_db
def test_director_can_view_and_save(client, django_user_model):
    client.force_login(_user(django_user_model, 1, rbac.ROLE_DIRECTOR))
    resp = client.get(reverse("admin_audit:retention_settings"))
    assert resp.status_code == 200
    assert DataRetentionPolicy.objects.count() == 5  # GET self-seeded all classes

    post = _full_post()
    post["days:audit"] = "30"
    post["on_leave:audit"] = "anonymise"
    resp = client.post(reverse("admin_audit:retention_settings"), post)
    assert resp.status_code == 302
    audit = DataRetentionPolicy.objects.get(data_class="audit")
    assert audit.retention_days == 30 and audit.on_member_leave == "anonymise" and audit.active
    assert AuditLog.objects.filter(action="retention.policy.update").exists()


@pytest.mark.django_db
def test_officer_and_member_blocked(client, django_user_model):
    client.force_login(_user(django_user_model, 2, rbac.ROLE_OFFICER))
    assert client.get(reverse("admin_audit:retention_settings")).status_code == 403
    client.force_login(_user(django_user_model, 3, rbac.ROLE_MEMBER))
    assert client.get(reverse("admin_audit:retention_settings")).status_code == 403


@pytest.mark.django_db
def test_unchecked_active_disables_only_that_row(client, django_user_model):
    client.force_login(_user(django_user_model, 4, rbac.ROLE_DIRECTOR))
    client.get(reverse("admin_audit:retention_settings"))
    post = _full_post()
    del post["active:audit"]  # unchecked → that row deactivates, others stay on
    client.post(reverse("admin_audit:retention_settings"), post)
    assert DataRetentionPolicy.objects.get(data_class="audit").active is False
    assert DataRetentionPolicy.objects.get(data_class="token").active is True


@pytest.mark.django_db
def test_blank_or_zero_days_keeps_current(client, django_user_model):
    client.force_login(_user(django_user_model, 5, rbac.ROLE_DIRECTOR))
    client.get(reverse("admin_audit:retention_settings"))
    DataRetentionPolicy.objects.filter(data_class="skill_snapshot").update(retention_days=200)
    post = _full_post(days="")  # blank → never silently delete-all
    post["days:token"] = "0"  # 0 also rejected → keep current
    client.post(reverse("admin_audit:retention_settings"), post)
    assert DataRetentionPolicy.objects.get(data_class="skill_snapshot").retention_days == 200
    assert DataRetentionPolicy.objects.get(data_class="token").retention_days == 365


@pytest.mark.django_db
def test_hub_links_retention_for_director_only(client, django_user_model):
    client.force_login(_user(django_user_model, 6, rbac.ROLE_DIRECTOR))
    assert b"/ops/admin/retention/settings/" in client.get(reverse("admin_audit:console")).content
    client.force_login(_user(django_user_model, 7, rbac.ROLE_OFFICER))
    assert b"/ops/admin/retention/settings/" not in client.get(reverse("admin_audit:console")).content
