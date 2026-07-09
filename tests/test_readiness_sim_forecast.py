"""Phase 6 — fleet simulator view + forecast findings (least-squares trend)."""
from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.characters.models import CharacterSkillSnapshot
from apps.identity.models import RoleAssignment
from apps.readiness.forecast import forecast_findings, linear_fit, project_breach
from apps.readiness.models import ReadinessFinding, ReadinessSnapshot, StrategicRoleTarget
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

GUNNERY = 3300


def _officer(django_user_model, name="off"):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    return user


def _member(django_user_model, cid, gunnery=0):
    user = django_user_model.objects.create(username=f"eve:{cid}")
    ch = EveCharacter.objects.create(
        character_id=cid, name=f"P{cid}", is_main=True, is_corp_member=True, user=user
    )
    CharacterSkillSnapshot.objects.create(
        character=ch, is_latest=True, skills={str(GUNNERY): {"trained_level": gunnery, "sp": 0}}
    )
    return ch


# --- pure least-squares helpers ----------------------------------------------
def test_linear_fit_and_breach():
    # A clean declining line y = 100 - 2x.
    pts = [(float(x), 100.0 - 2 * x) for x in range(10)]
    slope, intercept = linear_fit(pts)
    assert round(slope, 6) == -2.0 and round(intercept, 6) == 100.0
    # From the last point (x=9, y=82), crossing red=40 is 21 units of x away → but
    # within a 30-day window it's projected; within 14 it is not.
    assert project_breach(pts, red=40, window_days=30) == pytest.approx(21.0, abs=0.01)
    assert project_breach(pts, red=40, window_days=14) is None  # beyond window
    # An improving trend never breaches.
    rising = [(float(x), 50.0 + 2 * x) for x in range(10)]
    assert project_breach(rising, red=40, window_days=30) is None


def test_linear_fit_degenerate():
    assert linear_fit([(1.0, 2.0)]) is None          # too few points
    assert linear_fit([(1.0, 2.0), (1.0, 3.0)]) is None  # no x spread


# --- forecast findings -------------------------------------------------------
@pytest.mark.django_db
def test_forecast_emits_finding_on_declining_dimension(settings):
    # Eight snapshots with doctrine declining steeply toward its red band (40).
    now = timezone.now()
    for i in range(8):
        s = ReadinessSnapshot.objects.create(index=70, dimensions={"doctrine": 70 - i * 4})
        ReadinessSnapshot.objects.filter(pk=s.pk).update(
            created_at=now - dt.timedelta(days=(8 - i))
        )
    active = forecast_findings()
    assert active >= 1
    f = ReadinessFinding.objects.get(kind=ReadinessFinding.Kind.FORECAST, dimension_key="doctrine")
    assert f.predicted_breach_at is not None
    assert f.status == ReadinessFinding.Status.OPEN


@pytest.mark.django_db
def test_forecast_thin_history_no_finding():
    ReadinessSnapshot.objects.create(index=70, dimensions={"doctrine": 50})
    assert forecast_findings() == 0  # < 5 points


@pytest.mark.django_db
def test_forecast_resolves_when_trend_recovers():
    now = timezone.now()
    for i in range(8):
        s = ReadinessSnapshot.objects.create(index=70, dimensions={"doctrine": 70 - i * 4})
        ReadinessSnapshot.objects.filter(pk=s.pk).update(created_at=now - dt.timedelta(days=(8 - i)))
    forecast_findings()
    assert ReadinessFinding.objects.filter(kind="forecast", status="open").exists()
    # Add recovering snapshots so the trend no longer projects a breach.
    for i in range(8):
        s = ReadinessSnapshot.objects.create(index=80, dimensions={"doctrine": 60 + i * 3})
        ReadinessSnapshot.objects.filter(pk=s.pk).update(created_at=now + dt.timedelta(days=i))
    forecast_findings()
    f = ReadinessFinding.objects.get(kind="forecast", dimension_key="doctrine")
    assert f.status == ReadinessFinding.Status.RESOLVED


# --- simulator view ----------------------------------------------------------
@pytest.mark.django_db
def test_simulator_no_targets_placeholder(client, django_user_model):
    client.force_login(_officer(django_user_model))
    html = client.get("/readiness/sim/").content.decode()
    assert "No role targets to simulate" in html


@pytest.mark.django_db
def test_simulator_verdict_and_rows(client, django_user_model):
    _member(django_user_model, 9001, gunnery=5)  # qualifies
    _member(django_user_model, 9002, gunnery=5)  # qualifies
    _member(django_user_model, 9003, gunnery=1)
    StrategicRoleTarget.objects.create(
        role_key="logi", label="Logistics", desired_count=3,
        detection=StrategicRoleTarget.Detection.SKILLS, detection_params={"skills": {str(GUNNERY): 5}},
    )
    client.force_login(_officer(django_user_model, "off2"))
    html = client.get("/readiness/sim/").content.decode()
    assert "Can we field it?" in html
    # 2 qualified of 3 desired → fieldable 2 ≥ 60% of 3 → PARTIAL verdict.
    assert "PARTIAL" in html
    assert "Logistics" in html


@pytest.mark.django_db
def test_simulator_ready_when_all_met(client, django_user_model):
    _member(django_user_model, 9101, gunnery=5)
    _member(django_user_model, 9102, gunnery=5)
    StrategicRoleTarget.objects.create(
        role_key="logi", label="Logistics", desired_count=2,
        detection=StrategicRoleTarget.Detection.SKILLS, detection_params={"skills": {str(GUNNERY): 5}},
    )
    client.force_login(_officer(django_user_model, "off3"))
    html = client.get("/readiness/sim/").content.decode()
    assert "READY" in html


@pytest.mark.django_db
def test_simulator_is_officer_only(client, django_user_model):
    member = django_user_model.objects.create(username="m")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    assert client.get("/readiness/sim/").status_code == 403
