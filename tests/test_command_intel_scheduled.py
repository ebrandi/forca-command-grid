"""Scheduled report generation (P5, doc 18 P5): gating, dedupe, degraded generation."""
from __future__ import annotations

import datetime as dt

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.command_intel import config
from apps.command_intel.models import IntelligenceReport, Trigger
from apps.command_intel.scheduled import run_scheduled_report


@pytest.fixture(autouse=True)
def _clear_config_cache():
    # config.get caches the merged doc in locmem, which survives across tests; clear it so
    # a config.set in one test never leaks into another.
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
def test_disabled_is_inert():
    config.set("notifications", {"scheduled_enabled": False})
    assert run_scheduled_report() == "disabled"
    assert not IntelligenceReport.objects.filter(trigger=Trigger.SCHEDULED).exists()


@pytest.mark.django_db
def test_enabled_generates_a_scheduled_report(settings):
    # No key ⇒ deterministic ready_degraded (no network); the scheduled trigger is stamped.
    settings.COMMAND_INTEL_ENABLED = False
    config.set("notifications", {"scheduled_enabled": True, "deliver_discord": False,
                                 "deliver_evemail": False})
    result = run_scheduled_report()
    report = IntelligenceReport.objects.get(trigger=Trigger.SCHEDULED)
    assert result == f"{report.status}:{report.pk}"
    assert report.status == IntelligenceReport.Status.READY_DEGRADED


@pytest.mark.django_db
def test_dedupes_against_a_recent_scheduled_run(settings):
    settings.COMMAND_INTEL_ENABLED = False
    config.set("notifications", {"scheduled_enabled": True})
    recent = IntelligenceReport.objects.create(
        trigger=Trigger.SCHEDULED, status=IntelligenceReport.Status.READY_DEGRADED,
    )
    result = run_scheduled_report()
    assert result == f"deduped:{recent.pk}"
    # No second scheduled report was generated within the window.
    assert IntelligenceReport.objects.filter(trigger=Trigger.SCHEDULED).count() == 1


@pytest.mark.django_db
def test_a_held_lock_prevents_a_concurrent_run(settings):
    # MED-2: a redelivered/retried beat that overlaps an in-flight run must bail, not
    # double-generate (double token spend) or double-deliver.
    settings.COMMAND_INTEL_ENABLED = False
    config.set("notifications", {"scheduled_enabled": True})
    from django.core.cache import cache
    cache.add("command_intel:scheduled:lock", "1", 900)
    try:
        assert run_scheduled_report() == "locked"
        assert not IntelligenceReport.objects.filter(trigger=Trigger.SCHEDULED).exists()
    finally:
        cache.delete("command_intel:scheduled:lock")


@pytest.mark.django_db
def test_a_stale_scheduled_run_does_not_block(settings):
    settings.COMMAND_INTEL_ENABLED = False
    config.set("notifications", {"scheduled_enabled": True})
    old = IntelligenceReport.objects.create(
        trigger=Trigger.SCHEDULED, status=IntelligenceReport.Status.READY_DEGRADED,
    )
    IntelligenceReport.objects.filter(pk=old.pk).update(
        created_at=timezone.now() - dt.timedelta(days=10)
    )
    run_scheduled_report()
    # A fresh scheduled report is generated because the prior one is outside the window.
    assert IntelligenceReport.objects.filter(trigger=Trigger.SCHEDULED).count() == 2
