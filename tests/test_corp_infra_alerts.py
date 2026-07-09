"""CORP-3 (roadmap 2.3) — structure fuel / sov-ADM alerts + configurable thresholds.

Acceptance: thresholds are leadership-configurable; a structure crossing the fuel line
or a sov system below the ADM floor fires one deduped officer digest; an unchanged
breach set is a no-op; a return above threshold re-arms; the board flags reflect the
config; leadership can switch the event off.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.corporation.alerts import scan_infrastructure_alerts
from apps.corporation.models import CorpStructure, StructureAlertConfig
from apps.operations.models import SovStructure
from apps.pingboard import config
from apps.pingboard.models import Alert

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _reset():
    cache.delete(StructureAlertConfig._CACHE_KEY)
    config.reset("notifications")
    config.reset("general")
    yield
    cache.delete(StructureAlertConfig._CACHE_KEY)
    config.reset("notifications")
    config.reset("general")


def _alerts():
    return Alert.objects.filter(source_service="corporation")


def _structure(sid, *, days, name="Keepstar"):
    return CorpStructure.objects.create(
        structure_id=sid, type_id=35834, name=name,
        fuel_expires=timezone.now() + timedelta(days=days),
    )


def _sov(sid, *, adm, system="J-1"):
    return SovStructure.objects.create(
        structure_id=sid, alliance_id=1, solar_system_id=sid, system_name=system, adm=adm,
    )


def _cfg(*, fuel_days=3, adm_floor=3.0):
    StructureAlertConfig.objects.create(
        is_active=True, fuel_alert_days=fuel_days, adm_alert_floor=adm_floor
    )


def test_low_fuel_fires_one_digest_then_dedupes():
    _cfg()
    _structure(1, days=2)  # under 3
    assert scan_infrastructure_alerts()["status"] == "alerted"
    assert _alerts().count() == 1
    assert "fuel" in _alerts().first().body.lower()
    assert scan_infrastructure_alerts()["status"] == "unchanged"
    assert _alerts().count() == 1


def test_healthy_fuel_no_alert():
    _cfg()
    _structure(1, days=10)
    assert scan_infrastructure_alerts()["status"] == "ok"
    assert not _alerts().exists()


def test_out_of_fuel_wording():
    _cfg()
    _structure(1, days=0)
    scan_infrastructure_alerts()
    assert "out of fuel" in _alerts().first().body.lower()


def test_soft_adm_fires():
    _cfg(adm_floor=3.0)
    _sov(50, adm=2.0)
    scan_infrastructure_alerts()
    assert "adm" in _alerts().first().body.lower()


def test_fuel_threshold_is_configurable():
    # At default 3 a 4-day structure is fine; raise the threshold to 5 and it alerts.
    _cfg(fuel_days=5)
    _structure(1, days=4)
    assert scan_infrastructure_alerts()["status"] == "alerted"


def test_board_flags_reflect_config():
    _cfg(fuel_days=5, adm_floor=4.0)
    s = _structure(1, days=4)
    sov = _sov(50, adm=3.5)
    assert s.is_low_fuel is True   # 4 < 5
    assert sov.is_soft is True     # 3.5 < 4.0


def test_recovery_resets_and_realerts():
    _cfg()
    s = _structure(1, days=2)
    scan_infrastructure_alerts()
    assert _alerts().count() == 1
    s.fuel_expires = timezone.now() + timedelta(days=20)  # refuel
    s.save(update_fields=["fuel_expires"])
    assert scan_infrastructure_alerts()["status"] == "ok"
    s.fuel_expires = timezone.now() + timedelta(days=1)   # runs low again
    s.save(update_fields=["fuel_expires"])
    assert scan_infrastructure_alerts()["status"] == "alerted"
    assert _alerts().count() == 2


def test_disabled_event_is_a_noop():
    _cfg()
    _structure(1, days=1)
    config.set("notifications", {"events": {"corporation.infrastructure_alert": {"enabled": False}}})
    assert scan_infrastructure_alerts()["status"] == "disabled"
    assert not _alerts().exists()


def test_settings_console_renders_and_saves_for_officer(client, django_user_model):
    from django.urls import reverse

    from core import rbac
    from tests._raffle_utils import enrol_pilot

    user, _ = enrol_pilot(django_user_model, 7777, roles=(rbac.ROLE_OFFICER,))
    client.force_login(user)
    url = reverse("admin_audit:structure_alert_settings")
    got = client.get(url)
    assert got.status_code == 200
    assert b"Low-fuel warning" in got.content
    # Save a new threshold and confirm it persists.
    posted = client.post(url, {"fuel_alert_days": "5", "adm_alert_floor": "4.0"})
    assert posted.status_code in (302, 200)
    assert StructureAlertConfig.active().fuel_alert_days == 5
    # A crafted out-of-bounds fuel value is rejected server-side (would disable alerts).
    bad = client.post(url, {"fuel_alert_days": "0", "adm_alert_floor": "3.0"})
    assert bad.status_code == 200  # re-rendered with errors, not saved
    assert StructureAlertConfig.active().fuel_alert_days == 5  # unchanged


def test_thresholds_read_only_and_seeded_default():
    # With no explicitly-created config, the migration-seeded singleton (3 / 3.0) is used
    # and reading it never creates an extra row.
    from apps.corporation.models import StructureAlertConfig

    before = StructureAlertConfig.objects.count()
    assert StructureAlertConfig.thresholds() == (3.0, 3.0)
    assert StructureAlertConfig.objects.count() == before  # read-only, no write
