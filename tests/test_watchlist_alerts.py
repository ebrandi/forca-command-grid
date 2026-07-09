"""4.4 — Watchlist activity tripwire alerts.

Acceptance: an opt-in corp alert fires when a watched entity (pilot/corp/alliance) shows
up on a fresh killmail; per-entry cooldown dedup (no re-spam while still active, re-alert
after cooldown); off when the governance event is disabled or the watchlist's alerts
aren't armed; only entities inside the time window count.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.killboard.models import KillmailParticipant, Watchlist, WatchlistEntry
from apps.killboard.watchlist_alerts import scan_watchlist_activity
from apps.pingboard import config
from apps.pingboard.models import Alert
from tests._raffle_utils import home_kill

pytestmark = pytest.mark.django_db
HOSTILE_CHAR = 7001
HOSTILE_CORP = 660000


@pytest.fixture(autouse=True)
def _reset_config():
    config.reset("notifications")
    yield
    config.reset("notifications")


def _watchlist(entity_type="character", entity_id=HOSTILE_CHAR, *, alerts=True):
    wl = Watchlist.objects.create(name="Reds", alerts_enabled=alerts)
    WatchlistEntry.objects.create(watchlist=wl, entity_type=entity_type, entity_id=entity_id)
    return wl


def _alerts():
    return Alert.objects.filter(source_service="killboard")


def test_watched_character_on_killmail_alerts():
    _watchlist()
    home_kill(1, attackers=[(HOSTILE_CHAR, HOSTILE_CORP, True)])  # default when = 1h ago (in window)
    assert scan_watchlist_activity()["alerted"] == 1 and _alerts().count() == 1
    assert "risk indicator" in _alerts().first().body.lower()  # honest framing


def test_watched_corp_matches():
    _watchlist(entity_type="corporation", entity_id=HOSTILE_CORP)
    home_kill(2, attackers=[(9999, HOSTILE_CORP, True)])
    assert scan_watchlist_activity()["alerted"] == 1


def test_victim_side_also_matches():
    # A watched hostile we KILLED (victim participant) must also trip.
    _watchlist(entity_id=8001)
    km = home_kill(3, attackers=[(1, 111, True)])
    KillmailParticipant.objects.create(killmail=km, role=KillmailParticipant.Role.VICTIM,
                                       seq=99, character_id=8001, corporation_id=222, ship_type_id=587)
    assert scan_watchlist_activity()["alerted"] == 1


def test_disabled_event_is_noop():
    _watchlist()
    home_kill(4, attackers=[(HOSTILE_CHAR, HOSTILE_CORP, True)])
    config.set("notifications", {"events": {"killboard.watchlist_activity": {"enabled": False}}})
    assert scan_watchlist_activity()["status"] == "disabled"
    assert not _alerts().exists()


def test_alerts_disabled_watchlist_no_alert():
    _watchlist(alerts=False)
    home_kill(5, attackers=[(HOSTILE_CHAR, HOSTILE_CORP, True)])
    assert scan_watchlist_activity().get("alerted", 0) == 0 and not _alerts().exists()


def test_cooldown_dedup_and_reactivation():
    _watchlist()
    home_kill(6, attackers=[(HOSTILE_CHAR, HOSTILE_CORP, True)])
    assert scan_watchlist_activity()["alerted"] == 1
    home_kill(7, attackers=[(HOSTILE_CHAR, HOSTILE_CORP, True)])
    assert scan_watchlist_activity()["alerted"] == 0 and _alerts().count() == 1  # still cooling down
    entry = WatchlistEntry.objects.get()
    entry.last_alerted_at = timezone.now() - dt.timedelta(hours=7)  # past the 6h cooldown
    entry.save(update_fields=["last_alerted_at"])
    home_kill(8, attackers=[(HOSTILE_CHAR, HOSTILE_CORP, True)])
    assert scan_watchlist_activity()["alerted"] == 1  # re-fires after the cooldown
    entry.refresh_from_db()
    assert (timezone.now() - entry.last_alerted_at) < dt.timedelta(minutes=1)  # re-stamped


def test_same_entity_in_two_watchlists_alerts_once():
    _watchlist(entity_id=HOSTILE_CHAR)  # watchlist A
    _watchlist(entity_id=HOSTILE_CHAR)  # watchlist B, same hostile
    home_kill(20, attackers=[(HOSTILE_CHAR, HOSTILE_CORP, True)])
    assert scan_watchlist_activity()["alerted"] == 1 and _alerts().count() == 1  # not two
    # BOTH entries stamped, so neither re-fires on the next sweep.
    assert WatchlistEntry.objects.filter(last_alerted_at__isnull=False).count() == 2
    home_kill(21, attackers=[(HOSTILE_CHAR, HOSTILE_CORP, True)])
    assert scan_watchlist_activity()["alerted"] == 0


def test_unrelated_killmail_no_alert():
    _watchlist()
    home_kill(9, attackers=[(12345, 999999, True)])  # not the watched entity
    assert scan_watchlist_activity()["alerted"] == 0 and not _alerts().exists()


def test_killmail_outside_window_ignored():
    _watchlist()
    home_kill(10, attackers=[(HOSTILE_CHAR, HOSTILE_CORP, True)],
              when=timezone.now() - dt.timedelta(hours=5))  # outside the 90-min window
    assert scan_watchlist_activity()["alerted"] == 0
