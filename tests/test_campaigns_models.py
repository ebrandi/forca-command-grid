"""Campaign Command model tests: enums, defaults, __str__, and the DB-level constraints.

These pin what the *database* guarantees (named unique + check constraints, field defaults) so a
future migration that drops or renames one fails loudly. Stateful rules live in
``services.py`` and are covered by ``test_campaigns_services.py``.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.campaigns.models import (
    ActivitySource,
    Campaign,
    CampaignActivity,
    CampaignDependency,
    CampaignOperation,
    CampaignTemplate,
    DependencyKind,
    Issue,
    MeasurementSource,
    Objective,
    ObjectiveSample,
    Risk,
    Workstream,
)

pytestmark = pytest.mark.django_db


def _campaign(**kwargs) -> Campaign:
    return Campaign.objects.create(name=kwargs.pop("name", "Deployment"), **kwargs)


# --- defaults & enums ---------------------------------------------------------
def test_campaign_defaults_are_conservative():
    c = _campaign()
    assert c.status == Campaign.Status.DRAFT
    assert c.health == Campaign.Health.UNKNOWN
    # Drafts must never default member-visible (doc 06 §3.1).
    assert c.visibility == Campaign.Visibility.OFFICERS
    assert c.progress_mode == Campaign.ProgressMode.WEIGHTED
    assert c.recognition_mode == Campaign.RecognitionMode.NONE
    assert c.progress_pct == 0
    assert c.spent_isk == Decimal("0")
    assert c.health_reasons == []
    assert c.tags == []


def test_objective_defaults():
    c = _campaign()
    o = Objective.objects.create(campaign=c, title="Qualify 40 pilots")
    assert o.status == Objective.ObjectiveStatus.PENDING
    assert o.direction == Objective.Direction.GTE
    assert o.measurement_source == MeasurementSource.MANUAL
    assert o.weight == 1
    assert o.is_mandatory is False
    assert o.metric_params == {}


def test_enum_value_sets_match_the_spec():
    assert [s for s, _ in Campaign.Status.choices] == [
        "draft", "proposed", "approved", "active", "paused",
        "completed", "failed", "cancelled", "archived",
    ]
    assert {v for v, _ in Campaign.Category.choices} == {
        "doctrine_rollout", "deployment", "relocation", "defence_readiness", "stockpile",
        "srp_reserve", "membership", "training", "industry", "logistics", "coverage", "other",
    }
    assert [v for v, _ in DependencyKind.choices] == [
        "objective", "milestone", "workstream", "campaign", "external",
    ]
    assert [v for v, _ in MeasurementSource.choices] == ["auto", "manual"]
    assert [v for v, _ in ActivitySource.choices] == ["manual", "automation"]


def test_str_methods():
    c = _campaign(name="Armour BS Readiness")
    assert str(c) == "Armour BS Readiness"
    o = Objective.objects.create(campaign=c, title="Stock 30 hulls")
    assert str(o) == "Stock 30 hulls"
    ws = Workstream.objects.create(campaign=c, name="Logistics", key="logi")
    assert str(ws) == "Logistics"


# --- unique constraints -------------------------------------------------------
def test_workstream_key_unique_per_campaign():
    c = _campaign()
    Workstream.objects.create(campaign=c, name="Logi", key="logi")
    with transaction.atomic(), pytest.raises(IntegrityError):
        Workstream.objects.create(campaign=c, name="Logi 2", key="logi")


def test_workstream_key_may_repeat_across_campaigns():
    a, b = _campaign(), _campaign()
    Workstream.objects.create(campaign=a, name="Logi", key="logi")
    # Same key under a different campaign is fine (constraint is per-campaign).
    Workstream.objects.create(campaign=b, name="Logi", key="logi")


def test_dependency_edge_unique():
    c = _campaign()
    CampaignDependency.objects.create(
        campaign=c, from_kind=DependencyKind.OBJECTIVE, from_id=1,
        to_kind=DependencyKind.OBJECTIVE, to_id=2,
    )
    with transaction.atomic(), pytest.raises(IntegrityError):
        CampaignDependency.objects.create(
            campaign=c, from_kind=DependencyKind.OBJECTIVE, from_id=1,
            to_kind=DependencyKind.OBJECTIVE, to_id=2,
        )


def test_template_key_globally_unique():
    CampaignTemplate.objects.create(key="armour-bs", name="Armour BS")
    with transaction.atomic(), pytest.raises(IntegrityError):
        CampaignTemplate.objects.create(key="armour-bs", name="Armour BS Clone")


def test_campaign_operation_unique_per_campaign():
    c = _campaign()
    CampaignOperation.objects.create(campaign=c, operation_id=500)
    with transaction.atomic(), pytest.raises(IntegrityError):
        CampaignOperation.objects.create(campaign=c, operation_id=500)


# --- check constraints --------------------------------------------------------
def test_campaign_progress_bounded_to_100():
    with transaction.atomic(), pytest.raises(IntegrityError):
        Campaign.objects.create(name="x", progress_pct=101)


def test_objective_progress_bounded_to_100():
    c = _campaign()
    with transaction.atomic(), pytest.raises(IntegrityError):
        Objective.objects.create(campaign=c, title="x", progress_pct=250)


def test_spent_isk_cannot_be_negative():
    with transaction.atomic(), pytest.raises(IntegrityError):
        Campaign.objects.create(name="x", spent_isk=Decimal("-1"))


# --- composition & append-only shape -----------------------------------------
def test_cascade_delete_takes_children_with_the_campaign():
    c = _campaign()
    o = Objective.objects.create(campaign=c, title="obj")
    ObjectiveSample.objects.create(objective=o, value=Decimal("1"), measured_at=timezone.now())
    Issue.objects.create(campaign=c, objective=o, description="x")
    Risk.objects.create(campaign=c, description="x")
    CampaignActivity.objects.create(campaign=c, verb="status.changed")
    c.delete()
    assert Objective.objects.count() == 0
    assert ObjectiveSample.objects.count() == 0
    assert Issue.objects.count() == 0
    assert Risk.objects.count() == 0
    assert CampaignActivity.objects.count() == 0


def test_campaign_lead_migration_reverse_preserves_shared_permission():
    # The reverse migration detaches + deletes the seeded role but keeps campaign.manage when
    # another role still references it — never cascading a grant off a custom role (#35).
    import importlib

    from django.apps import apps as global_apps

    from apps.identity.models import Permission, Role

    mig = importlib.import_module("apps.campaigns.migrations.0002_seed_campaign_lead_role")
    perm = Permission.objects.get(key="campaign.manage")
    custom = Role.objects.create(key="custom_ops", label="Custom Ops", rank=0)
    custom.permissions.add(perm)

    mig.unseed(global_apps, None)

    assert not Role.objects.filter(key="campaign_lead").exists()       # seeded role removed
    assert Permission.objects.filter(key="campaign.manage").exists()   # still referenced → kept
    assert custom.permissions.filter(key="campaign.manage").exists()   # custom grant intact
