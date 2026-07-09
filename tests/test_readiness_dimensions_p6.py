"""Phase 6 — activity & recruitment dimensions + config tables (MandatoryShip/role targets)."""
from __future__ import annotations

import copy
import datetime as dt
from decimal import Decimal as D

import pytest
from django.utils import timezone

from apps.pilots.models import ContributionEvent
from apps.readiness import config
from apps.readiness.models import MandatoryShip, StrategicRoleTarget
from apps.readiness.services import compute_dimension, compute_readiness
from apps.sso.models import EveCharacter


def _member(django_user_model, cid, *, added_days_ago=200):
    user = django_user_model.objects.create(username=f"eve:{cid}")
    ch = EveCharacter.objects.create(
        character_id=cid, user=user, name=f"P{cid}", is_main=True, is_corp_member=True,
        added_at=timezone.now() - dt.timedelta(days=added_days_ago),
    )
    return user, ch


def _contrib(user, kind=ContributionEvent.Kind.FLEET, days_ago=5, ref=""):
    return ContributionEvent.objects.create(
        user=user, kind=kind, magnitude=D("1"), points=1,
        ref_type="t", ref_id=ref or f"{kind}-{user.id}-{days_ago}",
        occurred_at=timezone.now() - dt.timedelta(days=days_ago),
    )


# --- activity ----------------------------------------------------------------
@pytest.mark.django_db
def test_activity_unavailable_without_members():
    assert compute_dimension("activity").status == "unavailable"


@pytest.mark.django_db
def test_activity_scores_active_ratio(django_user_model):
    u1, _ = _member(django_user_model, 6001)
    u2, _ = _member(django_user_model, 6002)
    _member(django_user_model, 6003)  # inactive
    _contrib(u1)
    _contrib(u2)
    result = compute_dimension("activity")
    ratio = next(k for k in result.kpis if k.key == "activity.active_ratio")
    assert ratio.detail["active"] == 2 and ratio.detail["members"] == 3
    assert ratio.score == 67  # 2/3


@pytest.mark.django_db
def test_activity_low_engagement_emits_finding(django_user_model):
    _member(django_user_model, 6101)
    _member(django_user_model, 6102)
    _member(django_user_model, 6103)
    _member(django_user_model, 6104)  # all inactive → active_ratio 0
    result = compute_dimension("activity")
    assert any(f.kpi_key == "activity.active_ratio" for f in result.findings)


# --- recruitment -------------------------------------------------------------
@pytest.mark.django_db
def test_recruitment_scores_headcount_and_intake(django_user_model):
    u1, _ = _member(django_user_model, 6201, added_days_ago=10)  # recent join
    _member(django_user_model, 6202, added_days_ago=300)
    _contrib(u1)  # one active member
    # Lower the target so a 2-member corp can register meaningful scores.
    doc = copy.deepcopy(config.DEFAULTS["recruitment"])
    doc["target_active_members"] = 2
    doc["min_monthly_intake"] = 1
    config.set("recruitment", doc, user=None)

    result = compute_dimension("recruitment")
    intake = next(k for k in result.kpis if k.key == "recruitment.intake_rate")
    assert intake.value == 1  # one member joined in the last 30d
    assert result.score is not None


# --- index integration -------------------------------------------------------
@pytest.mark.django_db
def test_new_dims_disabled_by_default(django_user_model, sde):
    _member(django_user_model, 6301)
    result = compute_readiness(use_cache=False)
    assert "activity" not in result["dimensions"]
    assert "recruitment" not in result["dimensions"]


@pytest.mark.django_db
def test_enabling_activity_adds_it_to_index(django_user_model):
    _member(django_user_model, 6401)
    doc = copy.deepcopy(config.DEFAULTS["dimensions"])
    doc["activity"]["enabled"] = True
    config.set("dimensions", doc, user=None)
    result = compute_readiness(use_cache=False)
    assert "activity" in result["dimensions"]


# --- config tables -----------------------------------------------------------
@pytest.mark.django_db
def test_config_tables_exist_and_persist():
    MandatoryShip.objects.create(label="Travel Interceptor", category="travel", ship_type_id=11176)
    StrategicRoleTarget.objects.create(role_key="logi", label="Logistics", desired_count=12)
    assert MandatoryShip.objects.get().category == "travel"
    role = StrategicRoleTarget.objects.get()
    assert role.role_key == "logi" and role.desired_count == 12


@pytest.mark.django_db
def test_recruitment_config_validation():
    bad = copy.deepcopy(config.DEFAULTS["recruitment"])
    bad["max_dormant_ratio"] = 5  # out of 0..1
    with pytest.raises(config.ConfigError):
        config.set("recruitment", bad, user=None)
