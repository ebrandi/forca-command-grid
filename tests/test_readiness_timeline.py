"""Phase 6 UI — the readiness timeline view."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.readiness.models import ReadinessSnapshot
from apps.sso.services import ensure_role
from core import rbac


def _officer(django_user_model, name="off"):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    return user


def _snap(index, dims, days_ago):
    s = ReadinessSnapshot.objects.create(index=index, dimensions=dims)
    ReadinessSnapshot.objects.filter(pk=s.pk).update(
        created_at=timezone.now() - dt.timedelta(days=days_ago)
    )
    return s


@pytest.mark.django_db
def test_timeline_is_officer_only(client, django_user_model):
    member = django_user_model.objects.create(username="m")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    assert client.get("/readiness/timeline/").status_code == 403


@pytest.mark.django_db
def test_timeline_thin_history_shows_placeholder(client, django_user_model):
    client.force_login(_officer(django_user_model))
    _snap(70, {"doctrine": 70}, days_ago=1)  # only one point → not enough
    html = client.get("/readiness/timeline/").content.decode()
    assert "Not enough snapshots yet" in html


@pytest.mark.django_db
def test_timeline_renders_series_and_summary(client, django_user_model):
    client.force_login(_officer(django_user_model, "off2"))
    _snap(60, {"doctrine": 50, "financial": 40}, days_ago=20)
    _snap(72, {"doctrine": 80, "financial": 35}, days_ago=1)
    resp = client.get("/readiness/timeline/?range=90d")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "Readiness timeline" in html
    # The chart payload is embedded as JSON with both snapshots.
    start = html.index('id="rd-timeline-data"')
    blob = html[html.index(">", start) + 1: html.index("</script>", start)]
    data = json.loads(blob)
    assert len(data["series"]) == 2
    assert "doctrine" in data["dim_keys"] and "financial" in data["dim_keys"]
    # Net index moved +12; doctrine is the biggest gain, financial the biggest drop.
    assert "+12" in html


@pytest.mark.django_db
def test_timeline_range_filter_excludes_old(client, django_user_model):
    client.force_login(_officer(django_user_model, "off3"))
    _snap(50, {"doctrine": 50}, days_ago=200)  # outside 90d
    _snap(60, {"doctrine": 60}, days_ago=10)
    _snap(65, {"doctrine": 65}, days_ago=1)
    resp = client.get("/readiness/timeline/?range=90d")
    blob_start = resp.content.decode().index('id="rd-timeline-data"')
    html = resp.content.decode()
    blob = html[html.index(">", blob_start) + 1: html.index("</script>", blob_start)]
    data = json.loads(blob)
    assert len(data["series"]) == 2  # the 200-day-old snapshot is excluded
