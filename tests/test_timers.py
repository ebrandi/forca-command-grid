"""Structure / sov timer board."""
from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.operations.models import StructureTimer
from apps.sso.services import ensure_role
from core import rbac


def _user(django_user_model, cid, role):
    user = django_user_model.objects.create(username=f"eve:{cid}")
    RoleAssignment.objects.create(user=user, role=ensure_role(role))
    return user


@pytest.mark.django_db
def test_officer_adds_timer_in_utc(client, django_user_model):
    officer = _user(django_user_model, "t1", rbac.ROLE_OFFICER)
    client.force_login(officer)
    resp = client.post("/operations/timers/add/", {
        "name": "Fortizar — 1DQ1", "system_name": "1DQ1-A", "structure_type": "Fortizar",
        "timer_type": "armor", "side": "hostile", "exits_at": "2030-01-01T12:00",
        "notes": "form up 30 min prior",
    })
    assert resp.status_code == 302
    t = StructureTimer.objects.get()
    assert t.name == "Fortizar — 1DQ1" and t.timer_type == "armor" and t.side == "hostile"
    assert t.exits_at.tzinfo is not None and t.exits_at.year == 2030  # naive input → UTC


@pytest.mark.django_db
def test_member_sees_upcoming_timer(client, django_user_model):
    StructureTimer.objects.create(name="Astrahus Y", exits_at=timezone.now() + dt.timedelta(days=1))
    member = _user(django_user_model, "t2", rbac.ROLE_MEMBER)
    client.force_login(member)
    resp = client.get("/operations/timers/")
    assert resp.status_code == 200 and "Astrahus Y" in resp.content.decode()


@pytest.mark.django_db
def test_old_timers_hidden(client, django_user_model):
    StructureTimer.objects.create(name="OldOne", exits_at=timezone.now() - dt.timedelta(days=2))
    member = _user(django_user_model, "t3", rbac.ROLE_MEMBER)
    client.force_login(member)
    assert "OldOne" not in client.get("/operations/timers/").content.decode()


@pytest.mark.django_db
def test_officer_removes_timer(client, django_user_model):
    t = StructureTimer.objects.create(name="Z", exits_at=timezone.now() + dt.timedelta(days=1))
    officer = _user(django_user_model, "t4", rbac.ROLE_OFFICER)
    client.force_login(officer)
    assert client.post(f"/operations/timers/{t.pk}/remove/").status_code == 302
    assert not StructureTimer.objects.filter(pk=t.pk).exists()


# --- Pingboard consolidation: timers land on the calendar + multi-channel --------
@pytest.mark.django_db
def test_timer_add_publishes_to_calendar(client, django_user_model):
    """Adding a timer mirrors it onto the Pingboard calendar with its full detail."""
    from apps.pingboard.models import CalendarEvent

    client.force_login(_user(django_user_model, "tc1", rbac.ROLE_OFFICER))
    client.post("/operations/timers/add/", {
        "name": "Keepstar — X47", "system_name": "X47L-Q", "structure_type": "Keepstar",
        "timer_type": "hull", "side": "friendly", "exits_at": "2030-06-01T18:00",
        "notes": "cap fleet",
    })
    t = StructureTimer.objects.get()
    ev = CalendarEvent.objects.get(source_system="operations", source_object_id=f"timer:{t.pk}")
    assert ev.event_type == "structure_timer"
    assert ev.start_at == t.exits_at
    # The structure/system/side detail rides in the description (parity with the board).
    assert "X47L-Q" in ev.description and "Friendly" in ev.description and "Keepstar" in ev.description


@pytest.mark.django_db
def test_timer_remove_cancels_calendar_event(client, django_user_model):
    from apps.pingboard.models import CalendarEvent, CalendarEventStatus

    client.force_login(_user(django_user_model, "tc2", rbac.ROLE_OFFICER))
    client.post("/operations/timers/add/", {
        "name": "Astrahus Q", "timer_type": "armor", "side": "hostile",
        "exits_at": "2030-06-01T18:00",
    })
    t = StructureTimer.objects.get()
    client.post(f"/operations/timers/{t.pk}/remove/")
    ev = CalendarEvent.objects.get(source_system="operations", source_object_id=f"timer:{t.pk}")
    assert ev.status == CalendarEventStatus.CANCELLED


@pytest.mark.django_db
def test_sync_timers_publishes_manual_skips_autoimport_and_retires_orphans():
    """The sweep mirrors manual timers, skips ESI auto-imports (avoids double vs the
    CorpStructure sync), and cancels the mirror once a timer is gone."""
    from apps.pingboard import calendar as pcal
    from apps.pingboard.models import CalendarEvent, CalendarEventStatus

    soon = timezone.now() + dt.timedelta(hours=8)
    manual = StructureTimer.objects.create(name="Manual", exits_at=soon)
    auto = StructureTimer.objects.create(name="Auto", exits_at=soon,
                                         notes=StructureTimer.AUTO_IMPORT_NOTE)
    assert pcal._sync_timers() == 1  # only the manual one
    assert CalendarEvent.objects.filter(source_object_id=f"timer:{manual.pk}").exists()
    # the auto-imported timer is NOT mirrored here (it reaches the calendar via _sync_structures)
    assert not CalendarEvent.objects.filter(source_object_id=f"timer:{auto.pk}").exists()

    # Removing the timer and re-sweeping retires the orphaned mirror.
    pk = manual.pk
    manual.delete()
    pcal._sync_timers()
    ev = CalendarEvent.objects.get(source_object_id=f"timer:{pk}")
    assert ev.status == CalendarEventStatus.CANCELLED


@pytest.mark.django_db
def test_timer_announce_empty_selection_is_a_noop(client, django_user_model):
    """Ticking 'announce' but un-ticking every channel adds the timer WITHOUT broadcasting —
    an explicit 'send to none', never a fall-back fan-out to every armed channel."""
    from apps.pingboard.models import Alert

    client.force_login(_user(django_user_model, "tc4", rbac.ROLE_OFFICER))
    client.post("/operations/timers/add/", {
        "name": "Sotiyo Z", "timer_type": "armor", "side": "hostile",
        "exits_at": "2030-06-01T18:00", "announce": "1",  # announce on, but no channel_* ticked
    })
    t = StructureTimer.objects.get()
    assert not Alert.objects.filter(source_service="operations",
                                    source_object_id=f"timer:{t.pk}").exists()


@pytest.mark.django_db
def test_pingboard_can_add_structure_timer_with_full_detail(client, django_user_model):
    """Leadership can add a structure timer straight from the Pingboard calendar, with the
    same detail as the standalone board — it becomes a StructureTimer + a calendar event."""
    from apps.pingboard.models import CalendarEvent

    client.force_login(_user(django_user_model, "tc3", rbac.ROLE_OFFICER))
    resp = client.post("/pingboard/calendar/event/create/", {
        "title": "Fortizar — GE-8", "event_type": "structure_timer",
        "start_at": "2030-07-01T20:00", "system_name": "GE-8JV",
        "structure_type": "Fortizar", "timer_type": "armor", "side": "hostile",
        "description": "SRP on",
    })
    assert resp.status_code == 302
    t = StructureTimer.objects.get(name="Fortizar — GE-8")
    assert t.system_name == "GE-8JV" and t.timer_type == "armor" and t.side == "hostile"
    assert t.notes == "SRP on"  # description → notes for a timer
    ev = CalendarEvent.objects.get(source_system="operations", source_object_id=f"timer:{t.pk}")
    assert ev.event_type == "structure_timer"
