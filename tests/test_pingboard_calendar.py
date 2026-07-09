"""Phase 3 — the Pingboard Calendar: idempotent sync, override/conflict, reminders."""
from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.pingboard import calendar as cal
from apps.pingboard import config
from apps.pingboard.models import (
    AlertStatus,
    CalendarEvent,
    CalendarEventStatus,
    CalendarSyncEvent,
)


def _latest_action(event):
    return CalendarSyncEvent.objects.filter(event=event).latest("created_at").action


# --- publish / idempotency / conflict ----------------------------------------
@pytest.mark.django_db
def test_publish_creates_with_reminders_and_sync_record():
    start = timezone.now() + dt.timedelta(hours=3)
    e = cal.publish_event(source_system="operations", source_object_id="1",
                          event_type="fleet_op", title="Roam", start_at=start)
    assert e.pk and e.is_manual is False
    # fleet_op auto-attaches the configured reminder offsets
    assert sorted(e.alert_schedules.values_list("offset_minutes_before", flat=True)) == [15, 60, 1440]
    assert _latest_action(e) == "created"


@pytest.mark.django_db
def test_publish_is_idempotent():
    start = timezone.now() + dt.timedelta(hours=3)
    a = cal.publish_event(source_system="operations", source_object_id="1",
                          event_type="fleet_op", title="Roam", start_at=start)
    b = cal.publish_event(source_system="operations", source_object_id="1",
                          event_type="fleet_op", title="Roam v2", start_at=start)
    assert a.pk == b.pk
    assert CalendarEvent.objects.filter(source_system="operations", source_object_id="1").count() == 1
    b.refresh_from_db()
    assert b.title == "Roam v2" and _latest_action(b) == "updated"
    # a third identical publish is a noop
    cal.publish_event(source_system="operations", source_object_id="1",
                      event_type="fleet_op", title="Roam v2", start_at=start)
    assert _latest_action(b) == "noop"


@pytest.mark.django_db
def test_manual_edit_locks_field_against_sync():
    start = timezone.now() + dt.timedelta(hours=3)
    e = cal.publish_event(source_system="operations", source_object_id="1",
                          event_type="fleet_op", title="Roam", start_at=start)
    cal.edit_event(e, user=None, title="HUMAN TITLE")
    assert "title" in CalendarEvent.objects.get(pk=e.pk).locked_fields
    # sync tries to change the locked field → conflict, edit preserved
    cal.publish_event(source_system="operations", source_object_id="1",
                      event_type="fleet_op", title="sync override", start_at=start)
    e.refresh_from_db()
    assert e.title == "HUMAN TITLE"
    assert _latest_action(e) == "conflict"


@pytest.mark.django_db
def test_cancel_event():
    start = timezone.now() + dt.timedelta(hours=3)
    e = cal.publish_event(source_system="operations", source_object_id="1",
                          event_type="fleet_op", title="Roam", start_at=start)
    assert cal.cancel_event(source_system="operations", source_object_id="1") is True
    e.refresh_from_db()
    assert e.status == CalendarEventStatus.CANCELLED
    assert e.alert_schedules.filter(cancelled=False).count() == 0


# --- manual events + schedules -----------------------------------------------
@pytest.mark.django_db
def test_manual_event_and_schedule():
    start = timezone.now() + dt.timedelta(hours=6)
    e = cal.create_manual_event(title="Corp meeting", event_type="announcement", start_at=start)
    assert e.is_manual is True and e.source_system == ""
    s1 = cal.attach_alert_schedule(e, offset_minutes_before=60)
    s2 = cal.attach_alert_schedule(e, offset_minutes_before=60)  # dedup
    assert s1.pk == s2.pk
    assert cal.remove_alert_schedule(s1.pk) is True
    assert e.alert_schedules.count() == 0
    assert cal.cancel_manual_event(e) is True


# --- reminder materialisation ------------------------------------------------
@pytest.mark.django_db
def test_materialise_draft_until_approved(character):
    start = timezone.now() + dt.timedelta(minutes=30)
    e = cal.publish_event(source_system="operations", source_object_id="1",
                          event_type="fleet_op", title="Soon", start_at=start, auto_reminders=False)
    cal.attach_alert_schedule(e, offset_minutes_before=60)  # fire_at = start-60 = past ⇒ due
    out = cal.materialise_due_reminders()
    assert out == {"fired": 0, "drafted": 1}
    sched = e.alert_schedules.get()
    assert sched.alert_id is not None
    assert sched.alert.status == AlertStatus.DRAFT
    assert sched.alert.calendar_event_id == e.pk
    # approving queues it
    assert cal.approve_alert(sched.alert_id) is True
    sched.alert.refresh_from_db()
    assert sched.alert.status == AlertStatus.QUEUED


@pytest.mark.django_db
def test_materialise_auto_all_mode_queues(character):
    config.set("calendar", {"auto_alerts_mode": "auto_all"})
    start = timezone.now() + dt.timedelta(minutes=30)
    e = cal.publish_event(source_system="operations", source_object_id="1",
                          event_type="fleet_op", title="Soon", start_at=start, auto_reminders=False)
    cal.attach_alert_schedule(e, offset_minutes_before=60)
    out = cal.materialise_due_reminders()
    assert out == {"fired": 1, "drafted": 0}
    assert e.alert_schedules.get().alert.status == AlertStatus.QUEUED


@pytest.mark.django_db
def test_materialise_not_due_is_skipped():
    start = timezone.now() + dt.timedelta(hours=10)
    e = cal.publish_event(source_system="operations", source_object_id="1",
                          event_type="fleet_op", title="Later", start_at=start, auto_reminders=False)
    cal.attach_alert_schedule(e, offset_minutes_before=15)  # fire_at far future
    assert cal.materialise_due_reminders() == {"fired": 0, "drafted": 0}
    assert e.alert_schedules.get().alert_id is None


@pytest.mark.django_db
def test_materialise_idempotent(character):
    start = timezone.now() + dt.timedelta(minutes=30)
    e = cal.publish_event(source_system="operations", source_object_id="1",
                          event_type="fleet_op", title="Soon", start_at=start, auto_reminders=False)
    cal.attach_alert_schedule(e, offset_minutes_before=60)
    cal.materialise_due_reminders()
    # a second sweep must not create a second alert
    assert cal.materialise_due_reminders() == {"fired": 0, "drafted": 0}
    assert e.alert_schedules.get().alert is not None


# --- the source sweep --------------------------------------------------------
@pytest.mark.django_db
def test_sweep_operations_and_idempotency():
    from apps.operations.models import Operation

    now = timezone.now()
    op = Operation.objects.create(name="CTA", type="home_defence",
                                  target_at=now + dt.timedelta(hours=5), status="planned")
    res = cal.sync_calendar_sources()
    assert res["operations"] == 1
    ev = CalendarEvent.objects.get(source_system="operations", source_object_id=str(op.pk))
    assert ev.event_type == "emergency_fleet"  # home_defence → emergency_fleet
    # second sweep does not duplicate
    cal.sync_calendar_sources()
    assert CalendarEvent.objects.filter(source_system="operations", source_object_id=str(op.pk)).count() == 1
    # cancelling the op cancels the event
    op.status = "cancelled"
    op.save()
    cal.sync_calendar_sources()
    ev.refresh_from_db()
    assert ev.status == CalendarEventStatus.CANCELLED


@pytest.mark.django_db
def test_sweep_respects_publishing_services():
    from apps.operations.models import Operation

    config.set("calendar", {"publishing_services": []})  # nothing may publish
    Operation.objects.create(name="X", type="roam",
                             target_at=timezone.now() + dt.timedelta(hours=2), status="planned")
    assert cal.sync_calendar_sources() == {}
    assert CalendarEvent.objects.filter(source_system="operations").count() == 0


@pytest.mark.django_db
def test_sweep_disabled_globally():
    config.set("calendar", {"automated_sync_enabled": False})
    assert cal.sync_calendar_sources() == {"status": "disabled"}
