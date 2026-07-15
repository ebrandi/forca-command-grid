"""Pingboard UI (pilot + officer) and the inbound Telegram webhook.

Feature-gated with ``feature_required("pingboard")`` per-view (not the namespace
middleware) so the anonymous Telegram webhook stays reachable when the feature is off.
"""
from __future__ import annotations

import datetime as dt
import hmac
import json

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core import rbac
from core.audit import audit_log, client_ip
from core.features import feature_required

from . import calendar as pcal
from . import linking, preferences, services
from .models import (
    Alert,
    AlertCategory,
    AlertPriority,
    AlertStatus,
    CalendarEvent,
    CalendarEventStatus,
    CalendarEventType,
    CalendarSyncEvent,
    ChannelKind,
    ChannelProvider,
    PilotContactChannel,
)


# --- helpers -----------------------------------------------------------------
def _visible_events(user):
    qs = CalendarEvent.objects.exclude(status=CalendarEventStatus.CANCELLED)
    if rbac.has_role(user, rbac.ROLE_DIRECTOR):
        return qs
    if rbac.has_role(user, rbac.ROLE_OFFICER):
        return qs.filter(visibility__in=["member", "officer"])
    return qs.filter(visibility="member")


def _can_see_event(user, event) -> bool:
    return _visible_events(user).filter(pk=event.pk).exists()


# --- dashboard ---------------------------------------------------------------
@login_required
@feature_required("pingboard")
def dashboard(request):
    now = timezone.now()
    ctx = {
        "recent": Alert.objects.order_by("-created_at")[:8],
        "scheduled": Alert.objects.filter(status=AlertStatus.SCHEDULED).order_by("scheduled_at")[:8],
        "failed": Alert.objects.filter(
            status__in=[AlertStatus.FAILED, AlertStatus.PARTIAL]
        ).order_by("-created_at")[:8],
        "urgent": Alert.objects.filter(
            priority__in=[AlertPriority.URGENT, AlertPriority.EMERGENCY]
        ).order_by("-created_at")[:8],
        "upcoming": _visible_events(request.user).filter(start_at__gte=now).order_by("start_at")[:8],
        "failed_syncs": CalendarSyncEvent.objects.filter(
            action__in=["conflict", "failed"]
        ).order_by("-created_at")[:8],
        "is_officer": rbac.has_role(request.user, rbac.ROLE_OFFICER),
    }
    if ctx["is_officer"]:
        ctx["providers"] = ChannelProvider.objects.all()
    return render(request, "pingboard/dashboard.html", ctx)


# --- calendar ----------------------------------------------------------------
@login_required
@feature_required("pingboard")
def calendar(request):
    view = request.GET.get("view", "agenda")
    etype = request.GET.get("type", "")
    source = request.GET.get("source", "")
    events = _visible_events(request.user)
    if etype:
        events = events.filter(event_type=etype)
    if source:
        events = events.filter(source_system=source) if source != "manual" else events.filter(is_manual=True)

    now = timezone.now()
    month_grid = None
    if view == "month":
        try:
            anchor = dt.datetime.strptime(request.GET.get("month", ""), "%Y-%m").replace(tzinfo=dt.UTC)
        except ValueError:
            anchor = now.replace(day=1)
        month_grid = _build_month(events, anchor)
        ctx_events = None
    else:
        ctx_events = events.filter(start_at__gte=now - dt.timedelta(days=1)).order_by("start_at")[:100]

    from apps.operations.models import StructureTimer

    ctx = {
        "view": view, "events": ctx_events, "month": month_grid,
        "type": etype, "source": source,
        "event_types": CalendarEventType.choices,
        # Structure-timer form uses the same choices as the operations timer board, so a
        # timer added here carries the same detail as one added on /operations/timers/.
        "timer_types": StructureTimer.TimerType.choices,
        "sides": StructureTimer.Side.choices,
        "is_officer": rbac.has_role(request.user, rbac.ROLE_OFFICER),
    }
    return render(request, "pingboard/calendar.html", ctx)


def _build_month(events, anchor):
    first = anchor.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (first + dt.timedelta(days=32)).replace(day=1)
    rows = list(events.filter(start_at__gte=first - dt.timedelta(days=7),
                              start_at__lt=nxt + dt.timedelta(days=7)).order_by("start_at"))
    by_day: dict = {}
    for e in rows:
        by_day.setdefault(e.start_at.date(), []).append(e)
    # grid starts on the Monday on/before the 1st
    start = first - dt.timedelta(days=first.weekday())
    weeks = []
    day = start
    for _w in range(6):
        week = []
        for _d in range(7):
            week.append({"date": day.date(), "in_month": day.month == first.month,
                         "events": by_day.get(day.date(), [])})
            day += dt.timedelta(days=1)
        weeks.append(week)
        if day >= nxt and day.weekday() == 0:
            break
    prev_m = (first - dt.timedelta(days=1)).strftime("%Y-%m")
    next_m = nxt.strftime("%Y-%m")
    return {"weeks": weeks, "label": first.strftime("%B %Y"), "prev": prev_m, "next": next_m}


@login_required
@feature_required("pingboard")
def calendar_event(request, pk):
    event = get_object_or_404(CalendarEvent, pk=pk)
    if not _can_see_event(request.user, event):
        raise Http404
    ctx = {
        "event": event,
        "schedules": event.alert_schedules.select_related("alert").all(),
        "linked_alerts": event.alerts.order_by("-created_at")[:20],
        "sync_events": event.sync_events.order_by("-created_at")[:10],
        "is_officer": rbac.has_role(request.user, rbac.ROLE_OFFICER),
        "channel_kinds": ChannelKind.choices,
    }
    return render(request, "pingboard/event_detail.html", ctx)


# --- composer (officer) ------------------------------------------------------
@login_required
@feature_required("pingboard")
@rbac.role_required(rbac.ROLE_OFFICER)
def compose(request):
    from .models import AlertTemplate

    audiences = [("corp", gettext("Whole corporation")), ("role:officer", gettext("Officers")),
                 ("role:director", gettext("Directors"))]
    enabled_kinds = _enabled_channel_kinds()
    if request.method == "POST":
        return _compose_post(request, audiences, enabled_kinds)
    ctx = {
        "categories": AlertCategory.choices, "priorities": AlertPriority.choices,
        "audiences": audiences, "channel_kinds": enabled_kinds,
        "templates": AlertTemplate.objects.filter(enabled=True),
        "form": {}, "requirements": None,
    }
    return render(request, "pingboard/compose.html", ctx)


def _audience_spec(value: str) -> dict:
    if value.startswith("role:"):
        return {"kind": "role", "role": value.split(":", 1)[1]}
    return {"kind": "corp"}


def _compose_post(request, audiences, enabled_kinds):
    from .models import AlertTemplate

    p = request.POST
    category = p.get("category", "custom")
    priority = p.get("priority", "normal")
    audience = _audience_spec(p.get("audience", "corp"))
    channels = [k for k, _ in enabled_kinds if p.get(f"channel_{k}") == "on"]
    template_key = p.get("template", "")
    body = p.get("body", "")
    reason = p.get("reason", "")
    confirmation = {
        "two_step": p.get("confirm_two_step") == "on",
        "large_audience_ack": p.get("confirm_large") == "on",
        "by": request.user.get_username(), "at": timezone.now().isoformat(),
    }
    try:
        alert = services.emit_alert(
            category=category, priority=priority, title=p.get("title", "") or "Alert",
            body=body or None, template=(template_key or None),
            audience=audience, channels=channels or None, reason=reason,
            confirmation=confirmation, created_by=request.user, source="manual",
        )
    except ValueError as exc:
        req = services.dispatch_requirements(category, priority, audience)
        messages.error(request, str(exc))
        ctx = {
            "categories": AlertCategory.choices, "priorities": AlertPriority.choices,
            "audiences": audiences, "channel_kinds": enabled_kinds,
            "templates": AlertTemplate.objects.filter(enabled=True),
            "form": p, "requirements": req,
        }
        return render(request, "pingboard/compose.html", ctx)
    if alert is None:
        messages.warning(request, gettext("Alert suppressed (duplicate or rate-limited)."))
        return redirect("pingboard:compose")
    audit_log(request.user, "pingboard.alert.dispatched", target_type="pingboard_alert",
              target_id=str(alert.id), metadata={"category": category, "priority": priority},
              ip=client_ip(request))
    messages.success(request, gettext("Alert '%(title)s' created (%(status)s).") % {
        "title": alert.title, "status": alert.get_status_display()})
    return redirect("pingboard:alert_detail", pk=alert.id)


# --- history + alert detail (officer) ----------------------------------------
@login_required
@feature_required("pingboard")
@rbac.role_required(rbac.ROLE_OFFICER)
def history(request):
    qs = Alert.objects.order_by("-created_at")
    cat = request.GET.get("category", "")
    status = request.GET.get("status", "")
    priority = request.GET.get("priority", "")
    if cat:
        qs = qs.filter(category=cat)
    if status:
        qs = qs.filter(status=status)
    if priority:
        qs = qs.filter(priority=priority)
    ctx = {
        "alerts": qs[:100], "category": cat, "status": status, "priority": priority,
        "categories": AlertCategory.choices, "statuses": AlertStatus.choices,
        "priorities": AlertPriority.choices,
    }
    return render(request, "pingboard/history.html", ctx)


@login_required
@feature_required("pingboard")
@rbac.role_required(rbac.ROLE_OFFICER)
def alert_detail(request, pk):
    from .rendering_i18n import render_for

    alert = get_object_or_404(Alert, pk=pk)
    # In-app text is localised at VIEW time in the viewer's active language (the request
    # language is already activated by LocaleMiddleware). For a custom/legacy alert with no
    # re-render context, render_for returns the frozen title/body verbatim (D14.4/D14.6).
    subject, body = render_for(alert, request.LANGUAGE_CODE)
    ctx = {
        "alert": alert,
        "alert_subject": subject,
        "alert_body": body,
        "deliveries": alert.deliveries.select_related("provider").all(),
        "recipients": alert.recipients.all()[:200],
    }
    return render(request, "pingboard/alert_detail.html", ctx)


@login_required
@feature_required("pingboard")
@rbac.role_required(rbac.ROLE_OFFICER)
@require_POST
def alert_action(request, pk, action):
    alert = get_object_or_404(Alert, pk=pk)
    if action == "retry":
        services.retry_alert(alert.id, by=request.user)
        messages.success(request, gettext("Failed channels re-queued."))
    elif action == "cancel":
        services.cancel_alert(alert.id, by=request.user)
        messages.success(request, gettext("Alert cancelled."))
    elif action == "approve":
        if pcal.approve_alert(alert.id, by=request.user):
            messages.success(request, gettext("Alert approved and queued."))
        else:
            messages.error(request, gettext("Alert is not awaiting approval."))
    else:
        raise PermissionDenied
    return redirect("pingboard:alert_detail", pk=alert.id)


# --- calendar actions (officer) ----------------------------------------------
@login_required
@feature_required("pingboard")
@rbac.role_required(rbac.ROLE_OFFICER)
@require_POST
def event_create(request):
    start_raw = request.POST.get("start_at", "")
    start = _parse_dt(start_raw)
    if start is None:
        messages.error(request, gettext("A valid start time is required."))
        return redirect("pingboard:calendar")
    event_type = request.POST.get("event_type", CalendarEventType.CUSTOM)

    # A structure timer is the canonical operations StructureTimer (one source of truth):
    # create it there, then mirror it here. This lets leadership add a timer — with the
    # same system / structure / timer-type / side detail — straight from the calendar, and
    # it also shows on the /operations/timers/ countdown board.
    if event_type == CalendarEventType.STRUCTURE_TIMER:
        from apps.operations.services import (
            announce_structure_timer,
            create_structure_timer,
        )

        name = (request.POST.get("title") or "").strip()
        if not name:
            messages.error(request, gettext("A structure timer needs a name."))
            return redirect("pingboard:calendar")
        timer = create_structure_timer(
            name=name, exits_at=start,
            timer_type=request.POST.get("timer_type") or "",
            side=request.POST.get("side") or "",
            system_name=request.POST.get("system_name") or "",
            structure_type=request.POST.get("structure_type") or "",
            notes=request.POST.get("description") or "",
            created_by=request.user,
        )
        try:
            event = pcal.publish_timer(timer)
        except Exception:  # noqa: BLE001 - the timer is saved; the 10-min sweep will mirror it
            event = None
        if request.POST.get("announce") == "1":
            announce_structure_timer(timer, created_by=request.user)
        if event is None:
            messages.success(request, gettext("Structure timer added — it'll appear on the calendar shortly."))
            return redirect("pingboard:calendar")
        messages.success(request, gettext("Structure timer added — it's on the calendar and the timers board."))
        return redirect("pingboard:calendar_event", pk=event.id)

    event = pcal.create_manual_event(
        title=request.POST.get("title", "Event")[:200],
        event_type=event_type,
        start_at=start, description=request.POST.get("description", ""),
        visibility=request.POST.get("visibility", "member"), created_by=request.user,
    )
    messages.success(request, gettext("Calendar event created."))
    return redirect("pingboard:calendar_event", pk=event.id)


@login_required
@feature_required("pingboard")
@rbac.role_required(rbac.ROLE_OFFICER)
@require_POST
def event_action(request, pk, action):
    event = get_object_or_404(CalendarEvent, pk=pk)
    if action == "cancel":
        if event.is_manual:
            pcal.cancel_manual_event(event, user=request.user)
        else:
            pcal.cancel_event(source_system=event.source_system,
                              source_object_id=event.source_object_id, by=request.user)
        messages.success(request, gettext("Event cancelled."))
    elif action == "reminder":
        off = _int(request.POST.get("offset_minutes_before"), 60)
        pcal.attach_alert_schedule(event, offset_minutes_before=off,
                                   channels=event.default_channels or None, by=request.user)
        messages.success(request, gettext("Reminder scheduled %(minutes)d min before.") % {"minutes": off})
    else:
        raise PermissionDenied
    return redirect("pingboard:calendar_event", pk=event.id)


# --- pilot self-service channel linking --------------------------------------
@login_required
@feature_required("pingboard")
def my_channels(request):
    channels = list(PilotContactChannel.objects.filter(user=request.user))
    # A pending verify code is a bearer token: whoever sends it to the bot binds THEIR
    # chat id to this pilot's channel. Impersonation is read-only over HTTP, but the
    # redemption happens out-of-band via the provider's webhook, where the middleware
    # cannot reach. So never show a live code to a director who is viewing-as: they
    # could redeem it from their own Telegram and receive the pilot's DMs.
    # Blanked in memory only — the row is untouched.
    if getattr(request, "is_impersonating", False):
        for channel in channels:
            channel.verify_code = ""
    ctx = {
        "channels": channels,
        "kinds": PilotContactChannel.DM_KIND_CHOICES,
        "telegram_username": getattr(settings, "PINGBOARD_TELEGRAM_BOT_USERNAME", ""),
        "prefs": preferences.preference_matrix(request.user, channels),
    }
    return render(request, "pingboard/my_channels.html", ctx)


@login_required
@feature_required("pingboard")
@require_POST
def channel_prefs(request):
    """Save a pilot's per-category mute list for one linked DM channel.

    The form posts the categories the pilot wants to *keep* (checkbox = deliver);
    every mutable category absent from the post is muted. EMERGENCY is never in the
    form, so it can never be muted here.
    """
    kind = request.POST.get("kind", "")
    valid = {k for k, _ in PilotContactChannel.DM_KIND_CHOICES}
    if kind not in valid:
        messages.error(request, gettext("Unknown channel."))
        return redirect("pingboard:my_channels")
    keep = set(request.POST.getlist("deliver"))
    muted = [c.value for c in preferences.MUTABLE_ALERT_CATEGORIES if c.value not in keep]
    preferences.set_preferences(request.user, kind, muted)
    messages.success(request, gettext("Notification preferences saved."))
    return redirect("pingboard:my_channels")


@login_required
@feature_required("pingboard")
@require_POST
def channel_link(request):
    kind = request.POST.get("kind", "")
    handle = request.POST.get("handle", "").strip()
    valid = {k for k, _ in PilotContactChannel.DM_KIND_CHOICES}
    if kind not in valid:
        messages.error(request, gettext("Unknown channel."))
        return redirect("pingboard:my_channels")
    row = linking.start_link(request.user, kind, handle)
    if kind == "telegram":
        messages.info(request, gettext("Open the Telegram deep link below and press Start to verify."))
    else:
        messages.info(request, gettext("Verification code: %(code)s — confirm it below once received.") % {
            "code": row.verify_code})
    return redirect("pingboard:my_channels")


@login_required
@feature_required("pingboard")
@require_POST
def channel_confirm(request):
    kind = request.POST.get("kind", "")
    code = request.POST.get("code", "").strip()
    if linking.confirm(request.user, kind, code):
        messages.success(request, gettext("Channel verified."))
    else:
        messages.error(request, gettext("Wrong or expired code."))
    return redirect("pingboard:my_channels")


@login_required
@feature_required("pingboard")
@require_POST
def channel_unlink(request):
    linking.unlink(request.user, request.POST.get("kind", ""))
    messages.success(request, gettext("Channel removed."))
    return redirect("pingboard:my_channels")


# --- inbound Telegram webhook (anonymous) ------------------------------------
@csrf_exempt
@require_POST
def telegram_webhook(request, secret: str):
    """Inbound Telegram updates — verify a pilot's Telegram via ``/start <code>``.

    Authentication: a shared secret set via ``setWebhook``. Telegram sends it in the
    ``X-Telegram-Bot-Api-Secret-Token`` header (preferred — never lands in access
    logs); the legacy in-path segment is still accepted for an already-registered
    webhook URL. Both are compared with a constant-time ``hmac.compare_digest`` to
    avoid a byte-by-byte timing side-channel, and the endpoint fails closed when no
    secret is configured.
    """
    expected = settings.PINGBOARD_TELEGRAM_WEBHOOK_SECRET
    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    provided = header_secret or secret or ""
    # Compare as bytes: hmac.compare_digest on str raises TypeError for any non-ASCII
    # code point, which would turn an attacker-supplied non-ASCII secret header into an
    # uncaught 500 instead of a clean 403. Encoding first keeps the compare constant-time
    # and total over arbitrary input.
    if not expected or not hmac.compare_digest(provided.encode("utf-8", "ignore"),
                                               expected.encode("utf-8", "ignore")):
        return HttpResponseForbidden("bad webhook secret")
    try:
        update = json.loads((request.body or b"{}").decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": True})
    message = update.get("message") or update.get("edited_message") or {}
    text = (message.get("text") or "").strip()
    chat = (message.get("chat") or {}).get("id")
    if chat is not None and text.startswith("/start"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            linking.verify_by_code("telegram", parts[1].strip(), str(chat))
    return JsonResponse({"ok": True})


# --- small utils -------------------------------------------------------------
def _enabled_channel_kinds():
    """(kind, label) for channels that can actually deliver right now.

    Delegates to the shared :func:`services.enabled_channel_kinds` so the composer offers
    every armed channel — including Telegram/WhatsApp/Slack configured entirely in the
    Admin Console (creds on the ``ChannelProvider`` row, no env flag required).
    """
    return services.enabled_channel_kinds()


def _parse_dt(raw):
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(raw, fmt).replace(tzinfo=dt.UTC)
        except (ValueError, TypeError):
            continue
    return None


def _int(raw, default):
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default
