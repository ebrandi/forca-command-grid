"""Phase 6 — silent-service wiring: seeded rules, context_user audience, threshold sweep."""
from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.pingboard import automation, hooks
from apps.pingboard.models import Alert, AutomationRule


@pytest.mark.django_db
def test_seeded_rules_present_and_disabled():
    # the seed migration ships a rule per documented trigger source, all disabled
    assert AutomationRule.objects.count() >= 9
    assert not AutomationRule.objects.filter(enabled=True).exists()
    for key in ("srp-submitted", "srp-paid", "structure-fuel-low", "moon-fracture-ready",
                "industry-job-complete", "logistics-new", "store-new"):
        assert AutomationRule.objects.filter(key=key).exists()


@pytest.mark.django_db
def test_context_user_audience_targets_the_affected_pilot():
    rule = AutomationRule.objects.get(key="srp-approved")  # audience = context_user
    rule.enabled = True
    rule.channels = ["in_app"]
    rule.save()
    ids = hooks.fire("srp.approved", source_object_id="7", dedup_suffix="approved",
                     context={"target_user_id": 4242})
    assert ids
    assert Alert.objects.get(pk=ids[0]).audience == {"kind": "user", "id": 4242}


@pytest.mark.django_db
def test_hooks_fire_is_noop_without_enabled_rule():
    # nothing enabled → the hook is a cheap no-op and never raises
    assert hooks.fire("srp.submitted", source_object_id="1") == []
    assert not Alert.objects.filter(source="automation").exists()


@pytest.mark.django_db
def test_structure_fuel_threshold_fires_only_when_low():
    from apps.corporation.models import CorpStructure

    now = timezone.now()
    CorpStructure.objects.create(structure_id=1, type_id=35832, name="Low Fortizar",
                                 fuel_expires=now + dt.timedelta(days=2))
    CorpStructure.objects.create(structure_id=2, type_id=35832, name="Full Astrahus",
                                 fuel_expires=now + dt.timedelta(days=10))
    rule = AutomationRule.objects.get(key="structure-fuel-low")  # condition days_of_fuel_lt 3
    rule.enabled = True
    rule.channels = ["in_app"]
    rule.save()

    out = automation.evaluate_threshold_rules()
    assert out["structure_fuel"] == 1  # only the low structure fired
    assert Alert.objects.filter(source="automation", category="structure_timer").count() == 1


@pytest.mark.django_db
def test_threshold_sweep_noop_without_rule():
    from apps.corporation.models import CorpStructure

    CorpStructure.objects.create(structure_id=3, type_id=35832, name="X",
                                 fuel_expires=timezone.now() + dt.timedelta(days=1))
    # no enabled structure.fuel_low rule → no scan, no alert
    out = automation.evaluate_threshold_rules()
    assert out["structure_fuel"] == 0
    assert not Alert.objects.filter(source="automation").exists()
