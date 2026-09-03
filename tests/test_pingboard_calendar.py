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
    start = timezone.now() + dt.timedelta(days=2)  # every rung of the ladder is still ahead
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


# --- draft operations never reach the calendar (the op-9 incident) ------------
# Prod 2026-08-30: draft op 9 ("Mineração - Mercoxit") was swept onto the member calendar as
# a SYNCED event, which armed the fleet_op reminder ladder (1440/60/15) and — with prod's
# auto_alerts_mode="auto_all" — corp-broadcast three reminders for an op nobody had published.
def _op(status, **kw):
    from apps.operations.models import Operation

    return Operation.objects.create(
        name=kw.pop("name", "Mercoxit dig"), type=kw.pop("type", "mining"),
        target_at=kw.pop("target_at", timezone.now() + dt.timedelta(days=2)),
        status=status, **kw)


@pytest.mark.django_db
def test_sweep_skips_draft_operation_entirely():
    op = _op("draft")
    assert cal.sync_calendar_sources()["operations"] == 0
    assert not CalendarEvent.objects.filter(source_system="operations",
                                            source_object_id=str(op.pk)).exists()
    # no event ⇒ no reminder ladder ⇒ nothing for the materialiser to fire
    assert cal.materialise_due_reminders() == {"fired": 0, "drafted": 0}


@pytest.mark.django_db
def test_sweep_retracts_event_when_op_demoted_to_draft():
    """Demotion must retire the mirror, not just stop touching it — the prod row was already
    published, so a fix that merely skipped drafts would have left event 30 live forever."""
    op = _op("planned")
    cal.sync_calendar_sources()
    ev = CalendarEvent.objects.get(source_system="operations", source_object_id=str(op.pk))
    assert ev.alert_schedules.count() == 3  # fleet_op ladder armed while it was legitimately planned
    op.status = "draft"
    op.save()
    cal.sync_calendar_sources()
    ev.refresh_from_db()
    assert ev.status == CalendarEventStatus.CANCELLED
    assert ev.alert_schedules.filter(cancelled=False).count() == 0
    assert cal.materialise_due_reminders() == {"fired": 0, "drafted": 0}


@pytest.mark.django_db
def test_draft_to_planned_round_trip_restores_reminders():
    """The retraction must be reversible: cancel_event parks un-fired schedules at
    cancelled=True and nothing else ever clears it, so without the reinstatement in
    publish_event a demote/promote round trip would silently kill the ladder for good."""
    op = _op("planned")
    cal.sync_calendar_sources()
    ev = CalendarEvent.objects.get(source_system="operations", source_object_id=str(op.pk))
    op.status = "draft"
    op.save()
    cal.sync_calendar_sources()
    assert ev.alert_schedules.filter(cancelled=False).count() == 0
    op.status = "planned"
    op.save()
    cal.sync_calendar_sources()
    ev.refresh_from_db()
    assert ev.status == CalendarEventStatus.SYNCED
    assert ev.alert_schedules.filter(cancelled=False).count() == 3
    assert CalendarEvent.objects.filter(source_system="operations",
                                        source_object_id=str(op.pk)).count() == 1


@pytest.mark.django_db
def test_draft_operation_is_not_on_the_member_calendar(client, django_user_model):
    """The mirror was visibility="member", and _visible_events filters only on visibility —
    so the leak put the draft on every member's calendar page, not just in their DMs.

    The pilot needs real ROLE_MEMBER standing: without a RoleAssignment they rank as PUBLIC and
    MembershipGateMiddleware redirects /pingboard/ to onboarding, so the assertion would read an
    empty 302 body and pass no matter what. The published op is the positive control proving the
    page really is being rendered.
    """
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac

    draft = _op("draft", name="Secret staging")
    _op("planned", name="Public roam")
    cal.sync_calendar_sources()
    user = django_user_model.objects.create_user(username="grunt", password="x")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(user)
    resp = client.get("/pingboard/calendar/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Public roam" in body       # the page is genuinely rendering the calendar…
    assert "Secret staging" not in body  # …and the draft is absent from it
    assert not CalendarEvent.objects.filter(source_system="operations",
                                            source_object_id=str(draft.pk)).exists()


@pytest.mark.django_db
def test_approve_alert_refuses_a_retracted_event(character):
    """A reminder that already materialised survives cancel_event (which only reaches
    un-fired schedules), so approval is the last place a retraction can be caught."""
    start = timezone.now() + dt.timedelta(minutes=30)
    e = cal.publish_event(source_system="operations", source_object_id="1",
                          event_type="fleet_op", title="Soon", start_at=start,
                          auto_reminders=False)
    cal.attach_alert_schedule(e, offset_minutes_before=60)
    cal.materialise_due_reminders()
    sched = e.alert_schedules.get()
    cal.cancel_event(source_system="operations", source_object_id="1")
    assert cal.approve_alert(sched.alert_id) is False
    sched.alert.refresh_from_db()
    assert sched.alert.status == AlertStatus.DRAFT


@pytest.mark.django_db
def test_officer_cancel_survives_the_sweep_and_stays_unarmed(character, django_user_model):
    """An officer's "Cancel this event and its pending reminders?" must stick.

    The op itself is still planned, so the 10-minute sweep republishes the event — and a
    republish reinstates parked reminders. Without the ``status`` lock cancel_event takes on a
    human cancellation, that reinstatement would re-arm the very ladder the officer killed and
    (under prod's auto_alerts_mode="auto_all") broadcast it to the corp: the op-9 incident
    again, through a different door.
    """
    config.set("calendar", {"auto_alerts_mode": "auto_all"})
    officer = django_user_model.objects.create_user(username="fc", password="x")
    op = _op("planned", target_at=timezone.now() + dt.timedelta(minutes=30))
    cal.sync_calendar_sources()
    ev = CalendarEvent.objects.get(source_system="operations", source_object_id=str(op.pk))
    # 30 minutes out only the 15-minute rung is still ahead (the 24h/60m ones are skipped)
    assert ev.alert_schedules.filter(cancelled=False).count() == 1

    cal.cancel_event(source_system="operations", source_object_id=op.pk, by=officer)
    cal.sync_calendar_sources()
    ev.refresh_from_db()
    assert ev.status == CalendarEventStatus.CANCELLED
    assert "status" in ev.locked_fields
    assert ev.alert_schedules.filter(cancelled=False).count() == 0
    assert cal.materialise_due_reminders() == {"fired": 0, "drafted": 0}


# --- a DRAFT calendar event is unpublished ------------------------------------
def _draft_event(start=None, **kw):
    return cal.create_manual_event(
        title=kw.pop("title", "Secret staging"), event_type="fleet_op",
        start_at=start or (timezone.now() + dt.timedelta(minutes=30)),
        status=CalendarEventStatus.DRAFT, **kw)


@pytest.mark.django_db
def test_draft_event_reminders_never_materialise(character):
    """A draft event's ladder must stay silent until it is published — under prod's
    ``auto_all`` a materialised reminder goes straight to Discord, so DRAFT must not count
    as an open status for the materialiser."""
    config.set("calendar", {"auto_alerts_mode": "auto_all"})
    e = _draft_event()
    cal.attach_alert_schedule(e, offset_minutes_before=60)  # due right now
    assert cal.materialise_due_reminders() == {"fired": 0, "drafted": 0}
    assert e.alert_schedules.get().alert_id is None
    # …and the schedule is still armed, so publishing the event fires it as usual
    assert e.alert_schedules.get().cancelled is False
    cal.edit_event(e, status=CalendarEventStatus.SCHEDULED)
    assert cal.materialise_due_reminders() == {"fired": 1, "drafted": 0}


@pytest.mark.django_db
def test_approve_alert_refuses_a_reminder_whose_event_went_back_to_draft(character):
    start = timezone.now() + dt.timedelta(minutes=30)
    e = cal.create_manual_event(title="Soon", event_type="fleet_op", start_at=start)
    cal.attach_alert_schedule(e, offset_minutes_before=60)
    cal.materialise_due_reminders()
    sched = e.alert_schedules.get()
    cal.edit_event(e, status=CalendarEventStatus.DRAFT)
    assert cal.approve_alert(sched.alert_id) is False
    sched.alert.refresh_from_db()
    assert sched.alert.status == AlertStatus.DRAFT


@pytest.mark.django_db
def test_draft_event_is_hidden_from_members_but_not_officers(client, django_user_model):
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac

    draft = _draft_event(start=timezone.now() + dt.timedelta(hours=3))
    cal.create_manual_event(title="Public roam", event_type="fleet_op",
                            start_at=timezone.now() + dt.timedelta(hours=4))
    from apps.sso.models import EveCharacter

    member = django_user_model.objects.create_user(username="grunt", password="x")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=7101, user=member, name="Grunt", is_main=True,
                                is_corp_member=True)
    client.force_login(member)
    for url in ("/pingboard/calendar/", "/pingboard/"):
        body = client.get(url).content.decode()
        assert "Public roam" in body          # positive control: the page really renders
        assert "Secret staging" not in body
    assert client.get(f"/pingboard/calendar/{draft.pk}/").status_code == 404

    officer = django_user_model.objects.create_user(username="fc", password="x")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    EveCharacter.objects.create(character_id=7102, user=officer, name="FC", is_main=True,
                                is_corp_member=True)
    client.force_login(officer)
    assert "Secret staging" in client.get("/pingboard/calendar/").content.decode()
    assert client.get(f"/pingboard/calendar/{draft.pk}/").status_code == 200


# --- a late publish must not fire rungs whose moment has passed -----------------
@pytest.mark.django_db
def test_late_publish_attaches_only_the_rungs_still_ahead(character):
    """An op saved as Scheduled three hours before form-up used to get the whole ladder, and
    the materialiser fired the already-due 24h rung on its next tick — a corp-wide "in 24h"
    post within a minute of Save, before the officer (who may have meant a draft) could react.
    """
    config.set("calendar", {"auto_alerts_mode": "auto_all"})
    op = _op("planned", target_at=timezone.now() + dt.timedelta(hours=3))
    cal.sync_calendar_sources()
    ev = CalendarEvent.objects.get(source_system="operations", source_object_id=str(op.pk))
    assert sorted(ev.alert_schedules.values_list("offset_minutes_before", flat=True)) == [15, 60]
    assert cal.materialise_due_reminders() == {"fired": 0, "drafted": 0}
    # …and a second sweep does not sneak the skipped rung back in
    cal.sync_calendar_sources()
    assert ev.alert_schedules.count() == 2


@pytest.mark.django_db
def test_republish_reinstates_only_the_rungs_still_ahead(character):
    config.set("calendar", {"auto_alerts_mode": "auto_all"})
    op = _op("planned")  # two days out: full ladder
    cal.sync_calendar_sources()
    ev = CalendarEvent.objects.get(source_system="operations", source_object_id=str(op.pk))
    assert ev.alert_schedules.filter(cancelled=False).count() == 3
    op.status = "draft"
    op.save()
    cal.sync_calendar_sources()
    # time passes while the op sits in draft: form-up is now two hours away
    op.target_at = timezone.now() + dt.timedelta(hours=2)
    op.status = "planned"
    op.save()
    cal.sync_calendar_sources()
    ev.refresh_from_db()
    assert ev.status == CalendarEventStatus.SYNCED
    armed = sorted(ev.alert_schedules.filter(cancelled=False)
                   .values_list("offset_minutes_before", flat=True))
    assert armed == [15, 60]                       # the 24h rung stays parked
    assert cal.materialise_due_reminders() == {"fired": 0, "drafted": 0}


@pytest.mark.django_db
def test_reminder_body_states_the_time_actually_left(character):
    """A rung firing late (attached by hand already due) must not say "in 24h" for an event
    two hours away."""
    start = timezone.now() + dt.timedelta(hours=2, minutes=1)
    e = cal.create_manual_event(title="Roam", event_type="fleet_op", start_at=start)
    sched = cal.attach_alert_schedule(e, offset_minutes_before=1440)
    assert "(in 2h)" in cal._reminder_body(e, sched)
    cal.materialise_due_reminders()
    sched.refresh_from_db()
    assert sched.alert is not None and "(in 2h)" in sched.alert.body


@pytest.mark.django_db
def test_sweep_retires_the_mirror_of_an_op_that_lost_its_form_up_or_was_deleted():
    """The sweep visits only ops with a target_at, so a mirror whose op was deleted (or had
    its form-up cleared outside the form) was never retracted and kept its ladder live."""
    gone = _op("planned", name="Deleted later")
    cleared = _op("planned", name="Cleared later")
    kept = _op("planned", name="Still on")
    cal.sync_calendar_sources()
    assert CalendarEvent.objects.filter(source_system="operations",
                                        status=CalendarEventStatus.SYNCED).count() == 3
    gone_key, cleared_key = str(gone.pk), str(cleared.pk)
    gone.delete()
    cleared.target_at = None
    cleared.save()
    cal.sync_calendar_sources()
    by_key = {e.source_object_id: e for e in CalendarEvent.objects.filter(source_system="operations")}
    assert by_key[gone_key].status == CalendarEventStatus.CANCELLED
    assert by_key[cleared_key].status == CalendarEventStatus.CANCELLED
    assert by_key[str(kept.pk)].status == CalendarEventStatus.SYNCED
    assert by_key[gone_key].alert_schedules.filter(cancelled=False).count() == 0
    # a timer mirror is another source's business and must be left alone
    assert cal.materialise_due_reminders() == {"fired": 0, "drafted": 0}


@pytest.mark.django_db
def test_postponed_op_regains_the_rung_it_was_published_too_late_for(character):
    op = _op("planned", target_at=timezone.now() + dt.timedelta(hours=3))
    cal.sync_calendar_sources()
    ev = CalendarEvent.objects.get(source_system="operations", source_object_id=str(op.pk))
    assert sorted(ev.alert_schedules.values_list("offset_minutes_before", flat=True)) == [15, 60]
    op.target_at = timezone.now() + dt.timedelta(days=3)
    op.save()
    cal.sync_calendar_sources()
    armed = sorted(ev.alert_schedules.filter(cancelled=False)
                   .values_list("offset_minutes_before", flat=True))
    assert armed == [15, 60, 1440]


@pytest.mark.django_db
def test_op_pulled_earlier_parks_the_rung_whose_moment_passed(character):
    config.set("calendar", {"auto_alerts_mode": "auto_all"})
    op = _op("planned")  # two days out: full ladder armed
    cal.sync_calendar_sources()
    ev = CalendarEvent.objects.get(source_system="operations", source_object_id=str(op.pk))
    op.target_at = timezone.now() + dt.timedelta(hours=2)
    op.save()
    cal.sync_calendar_sources()
    armed = sorted(ev.alert_schedules.filter(cancelled=False)
                   .values_list("offset_minutes_before", flat=True))
    assert armed == [15, 60]
    assert cal.materialise_due_reminders() == {"fired": 0, "drafted": 0}
    # a rung that already fired is never touched by a reschedule
    fired = ev.alert_schedules.get(offset_minutes_before=1440)
    assert fired.alert_id is None and fired.cancelled is True
