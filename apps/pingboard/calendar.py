"""Pingboard Calendar — idempotent event sync + reminder materialisation.

Other services never write ``CalendarEvent`` directly; they (or the periodic sweep)
call ``publish_event``, keyed by ``(source_system, source_object_id)`` so repeated
syncs are idempotent. A human edit to a synced event *locks* those fields — automated
sync then records a ``conflict`` rather than clobbering the edit. Reminders attached to
an event materialise into alerts when due; per the locked decision they are
draft-until-approved by default (an officer approves before anything is sent).
"""
from __future__ import annotations

import datetime as dt
import logging

from django.utils import timezone

from core.audit import audit_log

from . import config
from .models import (
    CALENDAR_OPEN_STATUSES,
    Alert,
    AlertPriority,
    AlertStatus,
    CalendarEvent,
    CalendarEventAlert,
    CalendarEventStatus,
    CalendarEventType,
    CalendarSyncEvent,
)

log = logging.getLogger("forca.pingboard")

# Fields automated sync manages (and a human can lock against overwrite).
_SYNCED_FIELDS = ("title", "description", "event_type", "start_at", "end_at", "status")

_EVENT_CATEGORY = {
    CalendarEventType.FLEET_OP: "pvp_fleet",
    CalendarEventType.EMERGENCY_FLEET: "home_defence",
    CalendarEventType.MOON_EXTRACTION: "moon_extraction",
    CalendarEventType.MINING: "mining",
    CalendarEventType.STRUCTURE_TIMER: "structure_timer",
    CalendarEventType.INDUSTRY_JOB: "industry_job",
    CalendarEventType.LOGISTICS: "logistics",
    CalendarEventType.BUYBACK: "buyback",
    CalendarEventType.MENTORSHIP: "mentorship",
    CalendarEventType.ANNOUNCEMENT: "announcement",
    CalendarEventType.SCHEDULED_ALERT: "system",
    CalendarEventType.CUSTOM: "custom",
}


def _ser(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _record_sync(source_system, source_object_id, event, action, changed=None, error=""):
    CalendarSyncEvent.objects.create(
        source_system=source_system, source_object_id=str(source_object_id),
        event=event, action=action, changed_fields=changed or {}, error=(error or "")[:300],
    )


# --- publish / cancel (the internal sync API) --------------------------------
def publish_event(*, source_system, source_object_id, event_type, title, start_at,
                  end_at=None, description="", status=CalendarEventStatus.SYNCED,
                  visibility="member", audience=None, default_channels=None,
                  auto_reminders=True):
    """Idempotent upsert keyed by (source_system, source_object_id).

    Locked fields are never overwritten; a conflicting incoming value records a
    ``conflict`` sync event instead. Every call records a sync event.
    """
    if not source_system:
        raise ValueError("publish_event requires a source_system")
    sid = str(source_object_id)
    now = timezone.now()
    incoming = {
        "title": title, "description": description or "", "event_type": event_type,
        "start_at": start_at, "end_at": end_at, "status": status,
    }
    existing = CalendarEvent.objects.filter(source_system=source_system, source_object_id=sid).first()
    if existing is None:
        event = CalendarEvent.objects.create(
            source_system=source_system, source_object_id=sid, is_manual=False,
            last_synced_at=now, visibility=visibility, audience=audience or {},
            default_channels=default_channels or [], **incoming,
        )
        _record_sync(source_system, sid, event, "created",
                     {k: [None, _ser(v)] for k, v in incoming.items()})
        if auto_reminders:
            _auto_attach_reminders(event)
        return event

    locked = set(existing.locked_fields or [])
    changed, conflicts = {}, {}
    for field, newval in incoming.items():
        oldval = getattr(existing, field)
        if oldval == newval:
            continue
        if field in locked:
            conflicts[field] = [_ser(oldval), _ser(newval)]
            continue
        setattr(existing, field, newval)
        changed[field] = [_ser(oldval), _ser(newval)]
    existing.last_synced_at = now
    existing.save()
    if conflicts:
        _record_sync(source_system, sid, existing, "conflict", conflicts)
    elif changed:
        _record_sync(source_system, sid, existing, "updated", changed)
    else:
        _record_sync(source_system, sid, existing, "noop")
    return existing


def _timer_title(timer) -> str:
    return f"Timer — {timer.name} ({timer.get_timer_type_display()})"


def _timer_description(timer) -> str:
    parts = []
    if timer.system_name:
        parts.append(f"System: {timer.system_name}")
    if timer.structure_type:
        parts.append(f"Structure: {timer.structure_type}")
    parts.append(f"Timer: {timer.get_timer_type_display()}")
    parts.append(f"Side: {timer.get_side_display()}")
    if timer.notes:
        parts.append(timer.notes)
    return "\n".join(parts)


def publish_timer(timer):
    """Mirror an operations ``StructureTimer`` onto the calendar (idempotent, source-keyed).

    Member-visible to match the operations timer board (which members can already see);
    the structure/system/side details ride in the description. Auto-imported reinforcement
    timers are handled by ``_sync_structures`` instead — see ``_sync_timers``.
    """
    return publish_event(
        source_system="operations", source_object_id=f"timer:{timer.pk}",
        event_type=CalendarEventType.STRUCTURE_TIMER, title=_timer_title(timer),
        start_at=timer.exits_at, description=_timer_description(timer),
        status=CalendarEventStatus.SYNCED, visibility="member",
    )


def cancel_event(*, source_system, source_object_id, by=None) -> bool:
    sid = str(source_object_id)
    event = CalendarEvent.objects.filter(source_system=source_system, source_object_id=sid).first()
    if event is None or event.status == CalendarEventStatus.CANCELLED:
        return False
    event.status = CalendarEventStatus.CANCELLED
    event.last_synced_at = timezone.now()
    event.save(update_fields=["status", "last_synced_at", "updated_at"])
    event.alert_schedules.filter(alert__isnull=True, cancelled=False).update(cancelled=True)
    _record_sync(source_system, sid, event, "cancelled")
    return True


# --- manual events -----------------------------------------------------------
def create_manual_event(*, title, event_type, start_at, end_at=None, description="",
                        visibility="member", audience=None, default_channels=None,
                        status=CalendarEventStatus.SCHEDULED, created_by=None):
    event = CalendarEvent.objects.create(
        title=title, event_type=event_type, start_at=start_at, end_at=end_at,
        description=description or "", visibility=visibility, audience=audience or {},
        default_channels=default_channels or [], status=status, is_manual=True,
        created_by=created_by, updated_by=created_by,
    )
    audit_log(created_by, "pingboard.calendar.event_created",
              target_type="pingboard_calendar_event", target_id=str(event.id),
              metadata={"event_type": event_type, "manual": True})
    return event


def edit_event(event, *, user=None, lock=True, **fields):
    """Apply manual edits. For a *synced* event the edited fields become locked so a
    later automated sync records a conflict instead of overwriting the human change."""
    changed = []
    locked = set(event.locked_fields or [])
    for field, val in fields.items():
        if field in _SYNCED_FIELDS + ("visibility", "audience", "default_channels") \
                and getattr(event, field) != val:
            setattr(event, field, val)
            changed.append(field)
            if lock and not event.is_manual:
                locked.add(field)
    if not changed:
        return event
    event.updated_by = user
    event.locked_fields = sorted(locked)
    event.save()
    audit_log(user, "pingboard.calendar.event_updated",
              target_type="pingboard_calendar_event", target_id=str(event.id),
              metadata={"changed": changed, "override": bool(locked and not event.is_manual)})
    return event


def cancel_manual_event(event, *, user=None) -> bool:
    if event.status == CalendarEventStatus.CANCELLED:
        return False
    event.status = CalendarEventStatus.CANCELLED
    event.updated_by = user
    event.save(update_fields=["status", "updated_by", "updated_at"])
    event.alert_schedules.filter(alert__isnull=True, cancelled=False).update(cancelled=True)
    audit_log(user, "pingboard.calendar.event_cancelled",
              target_type="pingboard_calendar_event", target_id=str(event.id))
    return True


# --- reminder schedules ------------------------------------------------------
def attach_alert_schedule(event, *, offset_minutes_before, template=None, channels=None,
                          priority=AlertPriority.NORMAL, audience=None, by=None):
    sched, created = CalendarEventAlert.objects.get_or_create(
        event=event, offset_minutes_before=offset_minutes_before, template=template,
        defaults={"channels": channels or [], "priority": priority, "audience": audience or {}},
    )
    if created and by is not None:
        audit_log(by, "pingboard.calendar.alert_created",
                  target_type="pingboard_calendar_event", target_id=str(event.id),
                  metadata={"offset": offset_minutes_before})
    return sched


def remove_alert_schedule(schedule_id, *, by=None) -> bool:
    sched = CalendarEventAlert.objects.filter(pk=schedule_id).first()
    if sched is None:
        return False
    if sched.alert_id is None:
        sched.delete()
    else:
        sched.cancelled = True
        sched.save(update_fields=["cancelled", "updated_at"])
    if by is not None:
        audit_log(by, "pingboard.calendar.alert_cancelled",
                  target_type="pingboard_calendar_event", target_id=str(sched.event_id))
    return True


def _auto_attach_reminders(event) -> None:
    offsets = (config.get("calendar").get("reminder_offsets_minutes") or {}).get(event.event_type, [])
    for off in offsets:
        CalendarEventAlert.objects.get_or_create(
            event=event, offset_minutes_before=off, template=None,
            defaults={"channels": event.default_channels or [], "priority": AlertPriority.NORMAL},
        )


# --- reminder materialisation ------------------------------------------------
def _reminder_context(event) -> dict:
    return {
        "calendar_event_title": event.title,
        "calendar_event_start": f"{event.start_at:%Y-%m-%d %H:%M} EVE",
        "alert_category": event.get_event_type_display(),
    }


def _reminder_body(event, sched) -> str:
    when = f"{event.start_at:%a %d %b %H:%M} EVE"
    mins = sched.offset_minutes_before
    lead = f"in {mins} min" if mins < 60 else f"in {mins // 60}h"
    return f"⏰ {event.title} — {when} ({lead})."


def materialise_due_reminders() -> dict:
    """Turn due reminder schedules into alerts (draft-until-approved by default)."""
    from . import services

    now = timezone.now()
    cal = config.get("calendar")
    mode = cal.get("auto_alerts_mode", "draft_until_approved")
    threshold = config.get("anti_abuse").get("large_audience_threshold", 50)
    fired = drafted = 0
    qs = (CalendarEventAlert.objects
          .filter(alert__isnull=True, cancelled=False, event__status__in=CALENDAR_OPEN_STATUSES)
          .select_related("event", "template"))
    for sched in qs:
        event = sched.event
        fire_at = event.start_at - dt.timedelta(minutes=sched.offset_minutes_before)
        if fire_at > now:
            continue
        if event.start_at <= now:
            # the event already started — a late reminder is noise; retire the schedule
            sched.cancelled = True
            sched.save(update_fields=["cancelled", "updated_at"])
            continue
        audience = sched.audience or event.audience or None
        channels = sched.channels or event.default_channels or None
        if mode == "auto_all":
            dry = False
        elif mode == "auto_small":
            dry = services.recipient_estimate(audience) > threshold
        else:  # draft_until_approved (locked default)
            dry = True
        alert = services.emit_alert(
            category=_EVENT_CATEGORY.get(event.event_type, "system"),
            priority=sched.priority, title=event.title,
            body=_reminder_body(event, sched),
            template=(sched.template.key if sched.template else None),
            context=_reminder_context(event),
            audience=audience, channels=channels,
            source="automation", source_service="pingboard_calendar",
            source_object_id=str(event.id), calendar_event=event,
            idempotency_key=f"calevt:{event.id}:{sched.id}",
            bypass_ratelimit=True, dry_run=dry,
        )
        if alert is not None:
            sched.alert = alert
            sched.save(update_fields=["alert", "updated_at"])
            drafted += 1 if dry else 0
            fired += 0 if dry else 1
    return {"fired": fired, "drafted": drafted}


def approve_alert(alert_id, *, by=None) -> bool:
    """Approve a draft (calendar-generated) alert → queue it for delivery."""
    from .services import _enqueue

    alert = Alert.objects.filter(pk=alert_id, status=AlertStatus.DRAFT).first()
    if alert is None:
        return False
    alert.status = AlertStatus.QUEUED
    alert.approved_by = by
    alert.save(update_fields=["status", "approved_by", "updated_at"])
    audit_log(by, "pingboard.alert.approved", target_type="pingboard_alert", target_id=str(alert_id))
    _enqueue(alert.id)
    return True


# --- the source sweep (reliable regardless of how a source is written) --------
def sync_calendar_sources() -> dict:
    cal = config.get("calendar")
    if not cal.get("automated_sync_enabled", True):
        return {"status": "disabled"}
    services_on = set(cal.get("publishing_services") or [])
    out: dict = {}
    plan = [
        ("operations", "operations", _sync_operations),
        ("operations", "timer", _sync_timers),
        ("corporation", "moon", _sync_moon),
        ("corporation", "structure", _sync_structures),
        ("erp", "industry", _sync_industry),
        ("mentorship", "mentorship", _sync_mentorship),
        ("campaigns", "campaigns", _sync_campaigns),
    ]
    for service, label, fn in plan:
        if service not in services_on:
            continue
        try:  # one broken source must not break the rest of the calendar
            out[label] = fn()
        except Exception:  # noqa: BLE001
            log.exception("pingboard calendar source %s failed", label)
            out[label] = "error"
    return out


def _sync_operations() -> int:
    from apps.operations.models import Operation

    n = 0
    for op in Operation.objects.exclude(target_at__isnull=True):
        if op.status in {"cancelled", "cancelled_auto"}:
            cancel_event(source_system="operations", source_object_id=op.pk)
            continue
        etype = (CalendarEventType.EMERGENCY_FLEET
                 if op.type == "home_defence" else CalendarEventType.FLEET_OP)
        status = (CalendarEventStatus.COMPLETED if op.status == "done"
                  else CalendarEventStatus.SYNCED)
        publish_event(source_system="operations", source_object_id=op.pk, event_type=etype,
                      title=op.name or f"Operation {op.pk}", start_at=op.target_at,
                      status=status, visibility="member")
        n += 1
    return n


def _sync_timers() -> int:
    """Mirror the manual structure-timer board onto the calendar (+ retire removed/aged ones).

    Only *manual* timers — auto-imported reinforcement timers already reach the calendar
    via ``_sync_structures`` (from ``CorpStructure``), so republishing them here would
    double them up. Timers older than the board's 3h keep-window are cancelled, as are
    calendar mirrors whose timer was removed.
    """
    from apps.operations.models import StructureTimer

    now = timezone.now()
    cutoff = now - dt.timedelta(hours=3)
    live = (StructureTimer.objects.filter(exits_at__gte=cutoff)
            .exclude(notes=StructureTimer.AUTO_IMPORT_NOTE))
    live_keys = set()
    n = 0
    for t in live:
        publish_timer(t)
        live_keys.add(f"timer:{t.pk}")
        n += 1
    stale = (CalendarEvent.objects
             .filter(source_system="operations", source_object_id__startswith="timer:")
             .exclude(status=CalendarEventStatus.CANCELLED)
             .exclude(source_object_id__in=live_keys))
    for ev in stale:
        cancel_event(source_system="operations", source_object_id=ev.source_object_id)
    return n


def _sync_moon() -> int:
    from apps.corporation.models import MoonExtraction

    now = timezone.now()
    soonest = {}
    for ex in MoonExtraction.objects.filter(chunk_arrival__gte=now).order_by("chunk_arrival"):
        soonest.setdefault(ex.structure_id, ex)
    for sid, ex in soonest.items():
        name = ex.moon_name or ex.structure_name or f"Moon {sid}"
        publish_event(source_system="corporation", source_object_id=f"moon:{sid}",
                      event_type=CalendarEventType.MOON_EXTRACTION,
                      title=f"Moon extraction — {name}", start_at=ex.chunk_arrival,
                      status=CalendarEventStatus.SYNCED, visibility="officer")
    return len(soonest)


def _sync_structures() -> int:
    from apps.corporation.models import CorpStructure

    now = timezone.now()
    n = 0
    for s in CorpStructure.objects.filter(state_timer_end__isnull=False, state_timer_end__gte=now):
        publish_event(source_system="corporation", source_object_id=f"reinforce:{s.structure_id}",
                      event_type=CalendarEventType.STRUCTURE_TIMER,
                      title=f"Reinforcement — {s.name or s.structure_id}",
                      start_at=s.state_timer_end, status=CalendarEventStatus.SYNCED,
                      visibility="officer")
        n += 1
    return n


def _sync_industry() -> int:
    from apps.erp.models import CorpIndustryJob

    now = timezone.now()
    n = 0
    for j in CorpIndustryJob.objects.filter(status="active", end_date__isnull=False,
                                            end_date__gte=now):
        publish_event(source_system="erp", source_object_id=f"job:{j.job_id}",
                      event_type=CalendarEventType.INDUSTRY_JOB,
                      title=f"Industry job {j.job_id}", start_at=j.end_date,
                      status=CalendarEventStatus.SYNCED, visibility="officer")
        n += 1
    return n


def _sync_campaigns() -> int:
    """Publish ACTIVE (and APPROVED-with-dates) non-restricted campaigns' windows + milestone
    deadlines, retiring events whose campaign no longer qualifies. Implemented in
    ``apps.campaigns.calendar`` and lazy-imported here, matching the other sync sources."""
    from apps.campaigns.calendar import sync

    return sync()


def _sync_mentorship() -> int:
    from apps.mentorship.models import MentorshipSession

    now = timezone.now()
    n = 0
    for s in MentorshipSession.objects.filter(status="scheduled", scheduled_at__isnull=False,
                                              scheduled_at__gte=now):
        end = s.scheduled_at + dt.timedelta(minutes=s.duration_minutes or 30)
        publish_event(source_system="mentorship", source_object_id=s.pk,
                      event_type=CalendarEventType.MENTORSHIP,
                      title=s.topic or "Mentoring session", start_at=s.scheduled_at, end_at=end,
                      status=CalendarEventStatus.SYNCED, visibility="officer")
        n += 1
    return n
