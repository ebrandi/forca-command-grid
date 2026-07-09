"""MIN-3 (roadmap 3.13) — opt-in chunk-arrival reminder ping.

Reminders fire at configured offsets (default 24h + 1h) before a moon extraction's
chunk_arrival, at most once per (extraction, offset); stale offsets (a late-synced extraction)
are marked without firing; a disabled event fires nothing and leaves the marker so arming it
later still works.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.corporation.extractions import sweep_chunk_reminders
from apps.corporation.models import MoonExtraction

pytestmark = pytest.mark.django_db
_EMIT = "apps.pingboard.services.emit_broadcast"


@pytest.fixture(autouse=True)
def _capture(monkeypatch):
    calls: list = []
    monkeypatch.setattr(_EMIT, lambda **kw: calls.append(kw))
    return calls


def _ext(hours_until_arrival, *, sid=6001, moon="Amamake VI - Moon 1", sent=None):
    return MoonExtraction.objects.create(
        structure_id=sid, chunk_arrival=timezone.now() + timedelta(hours=hours_until_arrival),
        moon_name=moon, reminders_sent=sent or [])


def test_fires_at_24h_offset_when_due(_capture):
    ext = _ext(23.5)  # 24h fire-time was 30 min ago (fresh); 1h not due yet
    assert sweep_chunk_reminders() == 1
    assert len(_capture) == 1 and _capture[0]["source_service"] == "corporation"
    assert _capture[0]["audience"] == {"kind": "corp"}  # corp rally, not officer-only
    ext.refresh_from_db()
    assert ext.reminders_sent == [24]


def test_idempotent_no_refire(_capture):
    _ext(23.5)
    sweep_chunk_reminders()
    n = len(_capture)
    sweep_chunk_reminders()
    assert len(_capture) == n  # the 24h offset is already marked


def test_not_due_no_fire(_capture):
    _ext(48)  # neither 24h nor 1h offset is due yet
    assert sweep_chunk_reminders() == 0 and _capture == []


def test_1h_fires_near_arrival(_capture):
    ext = _ext(0.5, sent=[24])  # 24h already handled; 1h fire-time was 30 min ago (fresh)
    assert sweep_chunk_reminders() == 1
    ext.refresh_from_db()
    assert set(ext.reminders_sent) == {24, 1}


def test_stale_offset_marked_not_fired(_capture):
    # Extraction synced only 30 min before arrival: the 24h offset is long past (stale) → marked
    # without firing; the 1h offset is fresh → fires. No burst of past-due pings.
    ext = _ext(0.5)
    assert sweep_chunk_reminders() == 1  # only the fresh 1h reminder
    ext.refresh_from_db()
    assert set(ext.reminders_sent) == {24, 1}


def test_disabled_event_does_not_fire_or_mark(_capture):
    from apps.pingboard import config as pb_config

    doc = pb_config.get("notifications")
    doc["events"] = {**(doc.get("events") or {}), "mining.chunk_arrival": {"enabled": False}}
    pb_config.set("notifications", doc)
    ext = _ext(23.5)
    assert sweep_chunk_reminders() == 0 and _capture == []
    ext.refresh_from_db()
    assert ext.reminders_sent == []  # not advanced → arming it later still fires
