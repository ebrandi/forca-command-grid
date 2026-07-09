"""0.9: investigable audit log — filters, CSV export, protected retention floor."""
from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.admin_audit.models import AuditLog, DataRetentionPolicy
from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac


def _director(dum):
    u = dum.objects.create(username="dir", first_name="Director")
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_DIRECTOR))
    return u


@pytest.mark.django_db
def test_audit_filters_by_action_and_target(client, django_user_model):
    d = _director(django_user_model)
    client.force_login(d)
    AuditLog.objects.create(actor=d, action="sso.character_detached",
                            target_type="eve_character", target_id="42")
    AuditLog.objects.create(actor=d, action="access.friendly_corp.create",
                            target_type="friendly_corporation", target_id="99")

    html = client.get("/ops/audit/?action=detach").content.decode()
    assert "sso.character_detached" in html and "friendly_corp" not in html

    html2 = client.get("/ops/audit/?target=friendly").content.decode()
    assert "friendly_corp" in html2 and "character_detached" not in html2


@pytest.mark.django_db
def test_audit_filters_by_date(client, django_user_model):
    d = _director(django_user_model)
    client.force_login(d)
    now = timezone.now()
    old = AuditLog.objects.create(actor=d, action="old.action")
    AuditLog.objects.filter(pk=old.pk).update(created_at=now - dt.timedelta(days=40))
    AuditLog.objects.create(actor=d, action="recent.action")

    since = (now - dt.timedelta(days=7)).date().isoformat()
    html = client.get(f"/ops/audit/?from={since}").content.decode()
    assert "recent.action" in html and "old.action" not in html


@pytest.mark.django_db
def test_audit_csv_export_and_injection_guard(client, django_user_model):
    d = _director(django_user_model)
    client.force_login(d)
    AuditLog.objects.create(actor=d, action="test.action", target_type="t", target_id="=danger")

    resp = client.get("/ops/audit/?export=csv")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/csv")
    assert "attachment" in resp["Content-Disposition"]
    body = resp.content.decode()
    assert "test.action" in body
    # CSV-injection guard: a cell that starts with a formula trigger is quoted.
    assert "'=danger" in body


@pytest.mark.django_db
def test_audit_retention_floor(django_user_model):
    from apps.admin_audit.services import AUDIT_RETENTION_FLOOR_DAYS, enforce_retention

    assert AUDIT_RETENTION_FLOOR_DAYS >= 365
    now = timezone.now()

    def _log(days_old):
        a = AuditLog.objects.create(action="x")
        AuditLog.objects.filter(pk=a.pk).update(created_at=now - dt.timedelta(days=days_old))
        return a

    within_floor = _log(400)   # older than a 30-day policy but within the floor → KEEP
    beyond_floor = _log(AUDIT_RETENTION_FLOOR_DAYS + 30)  # beyond the floor → prune
    DataRetentionPolicy.objects.create(
        data_class=DataRetentionPolicy.DataClass.AUDIT, retention_days=30, active=True,
    )

    result = enforce_retention()
    ids = set(AuditLog.objects.values_list("pk", flat=True))
    assert within_floor.pk in ids        # protected by the floor despite the 30-day policy
    assert beyond_floor.pk not in ids    # beyond the floor → pruned
    assert result["audit"] == 1
