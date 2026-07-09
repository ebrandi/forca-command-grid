"""CMD-1 (roadmap 2.11) — auto-AAR on notable battles.

Acceptance: a new battle crossing a configured threshold auto-queues an AAR; ships off
(kill switch); one per battle; rate/per-run capped; directors set the thresholds.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.command_intel import config
from apps.command_intel.auto_aar import _crosses_threshold, scan_and_queue_aars
from apps.command_intel.models import BattleAnalysis
from apps.killboard.models import BattleReport
from core import rbac
from tests._raffle_utils import enrol_pilot

pytestmark = pytest.mark.django_db

_FACTS = "apps.command_intel.battle.battle_facts"


@pytest.fixture(autouse=True)
def _reset_battle_config():
    config.reset("battle")
    yield
    config.reset("battle")


# --- threshold logic (pure) --------------------------------------------------
def test_zero_threshold_never_matches():
    facts = {"totals": {"isk_swing": 999, "our_losses": 3, "logi_lost": 2, "off_doctrine_losses": 1}}
    cfg = {"auto_aar_min_isk_swing": 0, "auto_aar_min_our_losses": 0,
           "auto_aar_min_logi_lost": 0, "auto_aar_min_off_doctrine": 0}
    assert _crosses_threshold(facts, cfg) is False


def test_crosses_on_our_losses():
    facts = {"totals": {"isk_swing": 0, "our_losses": 6, "logi_lost": 0, "off_doctrine_losses": 0}}
    assert _crosses_threshold(facts, {"auto_aar_min_our_losses": 5}) is True


def test_crosses_on_absolute_isk_swing():
    facts = {"totals": {"isk_swing": -6_000_000_000, "our_losses": 0, "logi_lost": 0, "off_doctrine_losses": 0}}
    assert _crosses_threshold(facts, {"auto_aar_min_isk_swing": 5_000_000_000}) is True


# --- scan (integration) ------------------------------------------------------
def _battle():
    t = timezone.now() - timedelta(hours=1)
    return BattleReport.objects.create(title="Fight", system_ids=[30000142], start_time=t, end_time=t, sides={})


def _arm(**over):
    doc = {
        "auto_aar_enabled": True, "auto_aar_min_our_losses": 5, "auto_aar_min_isk_swing": 0,
        "auto_aar_min_logi_lost": 0, "auto_aar_min_off_doctrine": 0,
        "auto_aar_max_per_run": 3, "auto_aar_lookback_hours": 6,
    }
    doc.update(over)
    config.set("battle", doc)


def _crossing(monkeypatch):
    monkeypatch.setattr(_FACTS, lambda r: {"totals": {
        "our_losses": 10, "isk_swing": 0, "logi_lost": 0, "off_doctrine_losses": 0}})


def test_disabled_is_a_noop():
    _battle()
    assert scan_and_queue_aars()["status"] == "disabled"
    assert BattleAnalysis.objects.count() == 0


def test_crossing_battle_queues_one_aar(monkeypatch):
    report = _battle()
    _arm()
    _crossing(monkeypatch)
    assert scan_and_queue_aars()["queued"] == 1
    assert BattleAnalysis.objects.filter(battle_report_id=report.pk).count() == 1


def test_non_crossing_battle_is_skipped(monkeypatch):
    _battle()
    _arm(auto_aar_min_our_losses=50)  # threshold above the facts
    _crossing(monkeypatch)
    assert scan_and_queue_aars()["queued"] == 0
    assert BattleAnalysis.objects.count() == 0


def test_battle_with_existing_analysis_is_skipped(monkeypatch):
    report = _battle()
    BattleAnalysis.objects.create(battle_report_id=report.pk, status=BattleAnalysis.Status.READY)
    _arm()
    _crossing(monkeypatch)
    assert scan_and_queue_aars()["queued"] == 0
    assert BattleAnalysis.objects.filter(battle_report_id=report.pk).count() == 1  # unchanged


def test_per_run_cap_limits_queueing(monkeypatch):
    for _ in range(4):
        _battle()
    _arm(auto_aar_max_per_run=2)
    _crossing(monkeypatch)
    assert scan_and_queue_aars()["queued"] == 2


def test_console_director_saves(client, django_user_model):
    user, _ = enrol_pilot(django_user_model, 4400, roles=(rbac.ROLE_DIRECTOR,))
    client.force_login(user)
    url = reverse("admin_audit:command_intel_auto_aar")
    assert client.get(url).status_code == 200
    client.post(url, {
        "auto_aar_enabled": "on", "auto_aar_min_our_losses": "8", "auto_aar_min_isk_swing": "0",
        "auto_aar_min_logi_lost": "0", "auto_aar_min_off_doctrine": "0",
        "auto_aar_lookback_hours": "6", "auto_aar_max_per_run": "3",
    })
    cfg = config.get("battle")
    assert cfg["auto_aar_enabled"] is True
    assert cfg["auto_aar_min_our_losses"] == 8


def test_validator_upper_clamps_scan_scope():
    # A direct config.set (bypassing the UI clamps) can't widen the scan unboundedly.
    config.set("battle", {"auto_aar_lookback_hours": 1000, "auto_aar_max_per_run": 999})
    cfg = config.get("battle")
    assert cfg["auto_aar_lookback_hours"] == 168
    assert cfg["auto_aar_max_per_run"] == 50


def test_console_forbidden_below_director(client, django_user_model):
    user, _ = enrol_pilot(django_user_model, 4401, roles=(rbac.ROLE_OFFICER,))
    client.force_login(user)
    assert client.get(reverse("admin_audit:command_intel_auto_aar")).status_code in (302, 403, 404)
