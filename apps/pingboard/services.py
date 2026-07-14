"""Pingboard's public internal API — the only module other apps import.

Best-effort by contract: a Pingboard problem must never break the caller's business
action. ``emit_alert`` records the alert, runs anti-abuse gates, and enqueues async
delivery (record-then-deliver). Scheduled alerts are swept by the ``dispatch_due`` beat.
"""
from __future__ import annotations

import datetime as dt
import logging

from django.db import transaction
from django.utils import timezone, translation
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _lazy

from core.audit import audit_log
from core.i18n import broadcast_locale

from . import config, ratelimit, rendering
from .dispatch import AlertDispatcher, RecipientResolver
from .models import (
    ALERT_TERMINAL,
    Alert,
    AlertDelivery,
    AlertPriority,
    AlertSource,
    AlertStatus,
    AlertTemplate,
    DeliveryStatus,
)

log = logging.getLogger("forca.pingboard")

_URGENT = {AlertPriority.URGENT, AlertPriority.EMERGENCY}


def _enforce_dispatch_floor(gen: dict, user, priority, category: str) -> None:
    """Gate a *user-initiated* dispatch on the leadership-configured role floors.

    ``general.dispatch_floor`` maps each priority tier to the minimum role allowed to
    send it and ``general.announcement_floor`` guards corp-wide announcements. These
    are policy the Admin Console lets leadership set (default: officers do routine
    traffic, directors own urgent/emergency + announcements) but which — before this
    guard — was validated on write yet never enforced at dispatch, so any officer
    could exceed it via the composer.

    Enforced ONLY for manual, user-attributed dispatch. Service / scheduled /
    automation alerts are system-initiated and legitimately carry urgent/emergency
    priority (e.g. an ESI structure-attack ping), so they never reach here. Raises
    ``ValueError`` (surfaced by the composer as a form error) when the dispatcher is
    below the floor.
    """
    from core import rbac

    floors = gen.get("dispatch_floor") or {}
    required = floors.get(str(priority))
    if required and not rbac.has_role(user, required):
        raise ValueError(
            _("%(priority)s alerts require the %(role)s role.")
            % {"priority": str(priority).title(), "role": required}
        )
    if category == "announcement":
        ann = gen.get("announcement_floor", "director")
        if ann and not rbac.has_role(user, ann):
            raise ValueError(
                _("Corp-wide announcements require the %(role)s role.") % {"role": ann}
            )


def emit_alert(
    *,
    category: str,
    title: str,
    priority: str | None = None,
    body: str | None = None,
    template: str | AlertTemplate | None = None,
    context: dict | None = None,
    audience: dict | None = None,
    channels: list[str] | None = None,
    source: str | None = None,
    source_service: str = "",
    source_object_id: str = "",
    calendar_event=None,
    automation_rule=None,
    reason: str = "",
    confirmation: dict | None = None,
    scheduled_at=None,
    expires_at=None,
    created_by=None,
    idempotency_key: str = "",
    bypass_ratelimit: bool = False,
    dry_run: bool = False,
) -> Alert | None:
    """Create + enqueue an alert. Idempotent on ``idempotency_key``.

    Returns the ``Alert``, or ``None`` when suppressed (disabled / duplicate /
    rate-limited). Raises ``ValueError`` only for caller errors (missing urgent
    reason) — never for delivery problems.
    """
    gen = config.get("general")
    if not gen["enabled"]:
        return None

    if idempotency_key:
        existing = Alert.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return existing

    routing = config.get("routing").get("categories", {}).get(category, {})
    priority = priority or routing.get("priority") or AlertPriority.NORMAL
    if audience is None:
        audience = {"kind": routing.get("audience", "corp")}
    channels = channels or routing.get("channels") or gen["default_channels"]

    resolved_key = _resolve_template_key(template)
    # Freeze the audit body under the corp default broadcast locale so the stored
    # Alert.body is deterministic regardless of who/what triggered the emit. Per-recipient
    # locales are re-rendered later from template_key + context (dispatch / in-app view).
    #
    # The persisted context is flattened INSIDE the same override: a context slot must carry a raw
    # code / EVE / user datum (never a translated label — doc 08 §11.1), but should a call site ever
    # slip a gettext_lazy proxy in, freezing it here keeps it deterministic (the corp broadcast
    # locale, consistent with Alert.body) instead of silently capturing whatever locale the acting
    # officer's request happened to be in and shipping it to every recipient.
    with translation.override(broadcast_locale()):
        body_text, custom = _render_body(template, body, context)
        title_text = _render_title(title, context)
        context_frozen = _persist_context(context)

    src = source or (
        AlertSource.SCHEDULED if scheduled_at
        else AlertSource.SERVICE if source_service
        else AlertSource.MANUAL
    )
    if src == AlertSource.MANUAL and not gen.get("manual_alerts_enabled", True):
        return None
    if src == AlertSource.AUTOMATION and not gen.get("automated_alerts_enabled", True):
        return None

    # Governance gates run BEFORE consuming the dedup/rate-limit budget, so a refused
    # alert never "uses up" the window and block a corrected retry. Urgent/large-audience
    # confirmation is enforced only for officer-dispatched (manual) alerts.
    aa = config.get("anti_abuse")
    conf = confirmation or {}
    est = recipient_estimate(audience)
    if src == AlertSource.MANUAL:
        # Authorization first: a user-initiated dispatch may not exceed the
        # leadership-configured role floor for its priority/category. Skipped when
        # created_by is None (a programmatic/system manual create, not the officer
        # composer — which always attributes request.user), so the check gates the
        # actual escalation surface without blocking internal factory calls.
        if created_by is not None:
            _enforce_dispatch_floor(gen, created_by, priority, category)
        if priority in _URGENT and aa.get("require_reason_for_urgent", True) and not (reason or "").strip():
            raise ValueError(_("Urgent and emergency alerts require a reason."))
        if not dry_run:
            if priority in _URGENT and aa.get("two_step_urgent", True) and not conf.get("two_step"):
                raise ValueError(_("Urgent/emergency alerts require two-step confirmation."))
            if est > aa.get("large_audience_threshold", 50) and not conf.get("large_audience_ack"):
                raise ValueError(
                    _("This alert reaches %(count)s pilots; large-audience confirmation is "
                      "required.") % {"count": est}
                )
    approval_required = (
        src == AlertSource.MANUAL and category in (aa.get("approval_required_categories") or [])
    )

    # Duplicate + rate-limit gates (immediate sends + scheduled creation both count).
    dedup_hash = ratelimit.duplicate_hash(category, audience, body_text, source_object_id)
    if ratelimit.is_duplicate(dedup_hash):
        audit_log(created_by, "pingboard.alert.suppressed_duplicate",
                  target_type="pingboard_alert", target_id="",
                  metadata={"category": category})
        return None

    if not bypass_ratelimit:
        ok, why = ratelimit.try_consume_dispatch(getattr(created_by, "id", 0), category, priority)
        if not ok:
            audit_log(created_by, "pingboard.alert.rate_limited",
                      target_type="pingboard_alert", target_id="",
                      metadata={"category": category, "reason": why})
            return None

    if dry_run:
        status = AlertStatus.DRAFT
    elif scheduled_at:
        status = AlertStatus.SCHEDULED
    elif approval_required and not conf.get("approved_by"):
        status = AlertStatus.DRAFT  # held for a second-person approval
    else:
        status = AlertStatus.QUEUED

    if expires_at is None and gen.get("default_expiry_minutes"):
        base = scheduled_at or timezone.now()
        expires_at = base + dt.timedelta(minutes=int(gen["default_expiry_minutes"]))

    alert = Alert.objects.create(
        title=title_text[:200],
        body=body_text,
        category=category,
        priority=priority,
        severity=_severity_for(priority),
        source=src,
        status=status,
        created_by=created_by,
        scheduled_at=scheduled_at,
        expires_at=expires_at,
        audience=audience,
        channels=list(channels),
        reason=reason,
        confirmation=conf,
        recipient_count=est,
        template=_template_obj(template),
        custom_message=custom,
        template_key=resolved_key,
        context=context_frozen,
        automation_rule=automation_rule,
        source_service=source_service,
        source_object_id=str(source_object_id or ""),
        calendar_event=calendar_event,
        idempotency_key=idempotency_key,
        dedup_hash=dedup_hash,
    )
    audit_log(
        created_by, "pingboard.alert.created",
        target_type="pingboard_alert", target_id=str(alert.id),
        metadata={"category": category, "priority": priority, "source": src,
                  "channels": list(channels), "source_service": source_service,
                  "scheduled": bool(scheduled_at), "dry_run": dry_run},
    )

    if status == AlertStatus.QUEUED:
        _enqueue(alert.id)
    return alert


def dispatch_alert(alert_id: int) -> dict:
    """Synchronous delivery entry (the Celery task delegates here)."""
    return AlertDispatcher().dispatch(alert_id)


def housekeeping() -> dict:
    """Prune old terminal alerts, past calendar events and sync records (age-based, so a
    missed run self-heals). Retention windows are config knobs."""
    from .models import CalendarEvent, CalendarEventStatus, CalendarSyncEvent

    now = timezone.now()
    alert_days = int(config.get("general").get("alert_retention_days", 365))
    evt_days = int(config.get("calendar").get("event_retention_days", 90))
    alerts = Alert.objects.filter(
        status__in=ALERT_TERMINAL, created_at__lt=now - dt.timedelta(days=alert_days)
    ).delete()[0]
    events = CalendarEvent.objects.filter(
        start_at__lt=now - dt.timedelta(days=evt_days)
    ).exclude(status__in=[CalendarEventStatus.SCHEDULED, CalendarEventStatus.ACTIVE]).delete()[0]
    syncs = CalendarSyncEvent.objects.filter(
        created_at__lt=now - dt.timedelta(days=evt_days)
    ).delete()[0]
    return {"alerts": alerts, "events": events, "sync_events": syncs}


def cancel_alert(alert_id: int, *, by=None) -> bool:
    alert = Alert.objects.filter(pk=alert_id).first()
    if alert is None or alert.status in ALERT_TERMINAL:
        return False
    alert.status = AlertStatus.CANCELLED
    alert.save(update_fields=["status", "updated_at"])
    alert.deliveries.filter(status__in=[DeliveryStatus.PENDING, DeliveryStatus.RATE_LIMITED]).update(
        status=DeliveryStatus.SKIPPED, last_error="alert cancelled"
    )
    audit_log(by, "pingboard.alert.cancelled", target_type="pingboard_alert", target_id=str(alert_id))
    return True


def retry_alert(alert_id: int, *, by=None, kinds: list[str] | None = None) -> dict:
    """Officer-triggered manual retry of failed channels."""
    alert = Alert.objects.filter(pk=alert_id).first()
    if alert is None:
        return {"status": "missing"}
    qs = alert.deliveries.filter(status=DeliveryStatus.FAILED)
    if kinds:
        qs = qs.filter(kind__in=kinds)
    n = qs.update(status=DeliveryStatus.PENDING, last_error="")
    alert.retry_count += 1
    if alert.status in (AlertStatus.FAILED, AlertStatus.PARTIAL):
        alert.status = AlertStatus.QUEUED
    alert.save(update_fields=["retry_count", "status", "updated_at"])
    audit_log(by, "pingboard.alert.retried", target_type="pingboard_alert",
              target_id=str(alert_id), metadata={"channels": kinds or "all"})
    _enqueue(alert.id)
    return {"status": "requeued", "deliveries": n}


def dispatch_due_alerts() -> dict:
    """Sweep scheduled alerts whose time has come (the ``pingboard.dispatch_due`` beat)."""
    now = timezone.now()
    due = Alert.objects.filter(status=AlertStatus.SCHEDULED, scheduled_at__lte=now)
    fired = 0
    expired = 0
    for alert in due.iterator():
        if alert.expires_at and alert.expires_at <= now:
            Alert.objects.filter(pk=alert.pk, status=AlertStatus.SCHEDULED).update(
                status=AlertStatus.EXPIRED
            )
            expired += 1
            continue
        updated = Alert.objects.filter(pk=alert.pk, status=AlertStatus.SCHEDULED).update(
            status=AlertStatus.QUEUED
        )
        if updated:
            AlertDispatcher().dispatch(alert.pk)
            fired += 1
    return {"fired": fired, "expired": expired}


def retry_failed_deliveries() -> dict:
    """Re-dispatch alerts with retryable failed deliveries under the attempt cap.

    Moves FAILED/PARTIAL alerts back to QUEUED so the dispatcher re-runs; delivered
    channels are skipped (deliver-once) and only the failing channels are retried,
    up to each delivery's ``max_attempts`` (after which they converge to FAILED).
    """
    from django.db.models import F

    now = timezone.now()
    retryable = (
        AlertDelivery.objects.filter(status=DeliveryStatus.FAILED, attempts__lt=F("max_attempts"))
        .exclude(alert__status=AlertStatus.CANCELLED)
        .values_list("alert_id", flat=True)
        .distinct()
    )
    n = 0
    for alert_id in list(retryable):
        moved = Alert.objects.filter(
            pk=alert_id, status__in=[AlertStatus.FAILED, AlertStatus.PARTIAL]
        ).update(status=AlertStatus.QUEUED)
        if moved:
            AlertDispatcher().dispatch(alert_id)
            n += 1
    return {"retried": n, "at": now.isoformat()}


# --- channel discovery + multi-channel broadcast -----------------------------
# The chat channels a plain-text broadcast can post to (a configured webhook / group /
# channel destination). In-app + EVE-mail are per-recipient and reached via emit_alert.
BROADCAST_CHAT_KINDS = ("discord", "slack", "telegram", "whatsapp")

# DM/global-token channels are ARMED via the web UI (creds on the ChannelProvider row)
# OR a legacy env flag. Everything else appears purely from an enabled ChannelProvider.
_ENV_CHANNEL_FLAGS = {
    "slack": "PINGBOARD_SLACK_ENABLED",
    "telegram": "PINGBOARD_TELEGRAM_ENABLED",
    "whatsapp": "PINGBOARD_WHATSAPP_ENABLED",
}

# Classification tiers a channel may carry, low→high. A blank provider ceiling means
# "no cap" (backward-compatible); an unranked message tier is treated as corp_internal.
_CLASSIFICATION_RANK = {
    "corp_internal": 0, "member": 0,
    "high_command": 1, "officer": 1, "officers_only": 1,
    "director_eyes_only": 2, "alliance_command": 2, "director": 2, "admin": 2,
}


def enabled_channel_kinds() -> list[tuple[str, str]]:
    """``(kind, label)`` for every channel that can actually deliver right now.

    In-app is always available. A network channel appears when an operator has armed an
    enabled ``ChannelProvider`` of that kind (the web-UI config model added in the
    Telegram/WhatsApp web-config work) OR — for the bot-token DM channels — when the
    legacy ``PINGBOARD_*_ENABLED`` env flag is set. This drives the composer's channel
    list and any service's channel picker, so a channel armed in the Admin Console is
    never hidden just because no env var was exported.
    """
    from django.conf import settings

    from .models import ChannelKind, ChannelProvider

    labels = dict(ChannelKind.choices)
    armed = set(
        ChannelProvider.objects.filter(enabled=True).values_list("kind", flat=True).distinct()
    )
    out = [("in_app", labels["in_app"])]
    for kind in ("eve_mail", "discord", "slack", "telegram", "whatsapp"):
        flag = _ENV_CHANNEL_FLAGS.get(kind)
        if kind in armed or (flag and getattr(settings, flag, False)):
            out.append((kind, labels[kind]))
    return out


def enabled_channel_values() -> list[str]:
    """Just the ``kind`` values of :func:`enabled_channel_kinds` — the default fan-out
    for a service that wants to reach every channel the corporation has armed."""
    return [k for k, _ in enabled_channel_kinds()]


def audience_classification(audience: dict | None) -> str | None:
    """The classification an alert's *audience* implies, for the broadcast-channel guard.

    A restricted-audience alert (officers, directors, a named-pilot DM) must never be
    posted to a shared "mass" chat destination (a corp Discord webhook, a Slack channel,
    a Telegram/WhatsApp group) whose classification ceiling doesn't clear it — the sink
    honours the same ceiling the config page exposes. Corp/public/member audiences imply
    ``None`` (corp-internal → any channel). Per-recipient legs (in-app, EVE-mail, verified
    DM handles) always deliver to the addressed pilots and are never gated by this.
    """
    from .notifications import classification_for_audience

    kind = (audience or {}).get("kind", "corp")
    if kind == "role":
        return classification_for_audience((audience or {}).get("role", "officer"))
    return classification_for_audience(kind)


def _classification_ok(provider_ceiling: str, classification: str | None) -> bool:
    """Whether a message of ``classification`` may go to a channel capped at
    ``provider_ceiling``.

    Fail-safe: a blank or unrecognised ceiling is treated as ``corp_internal`` (the
    ``ChannelProvider.max_classification`` documented default), so an ordinary corp
    channel never carries officer-tier (``high_command``) or higher intel. A director must
    explicitly raise a channel's ceiling to ``high_command`` for it to receive that tier.
    An unclassified message (``None``) is treated as ``corp_internal`` and reaches any
    channel — so ordinary corp/fleet alerts are unaffected.
    """
    msg = _CLASSIFICATION_RANK.get(classification or "corp_internal", 0)
    cap = _CLASSIFICATION_RANK.get(provider_ceiling or "corp_internal", 0)
    return msg <= cap


def broadcast_text(
    text: str, *, subject: str = "", classification: str | None = None,
) -> int:
    """Post a plain-text message to every enabled broadcast chat channel.

    The multi-channel successor to ``recommendations.broadcast_discord``: same
    fire-and-forget, count-returning contract (0 = nothing delivered), but it fans out to
    Discord + Slack + Telegram + WhatsApp group/channel destinations instead of Discord
    alone. Each provider is best-effort and isolated (one dead channel never blocks the
    rest). A provider whose ``max_classification`` ceiling is below ``classification`` is
    skipped, so Command Intelligence's classification guard holds across every channel.
    Returns 0 when nothing is armed (the legacy NotificationChannel fallback is retired).
    """
    from .models import ChannelProvider
    from .providers import provider_class

    rows = list(
        ChannelProvider.objects.filter(kind__in=BROADCAST_CHAT_KINDS, enabled=True)
    )
    if not rows:
        return 0

    sent = 0
    for row in rows:
        if not _classification_ok(row.max_classification, classification):
            continue
        pcls = provider_class(row.kind)
        if pcls is None:
            continue
        try:
            result = pcls(row).send(subject=subject, body=text, recipients=[])
        except Exception:  # noqa: BLE001 - a provider must never crash a broadcast
            log.exception("pingboard broadcast_text: provider %s crashed", row.kind)
            continue
        if result.ok:
            sent += 1
    return sent


def emit_broadcast(
    *, category: str, title: str, body: str, source_service: str,
    source_object_id: str = "", priority: str | None = None, reason: str = "",
    audience: dict | None = None, channels: list[str] | None = None,
    idempotency_key: str = "", created_by=None,
    template: str | AlertTemplate | None = None, context: dict | None = None,
) -> Alert | None:
    """Service entry point: emit an alert across every armed channel by default.

    A migrated service passes ``template`` (a ``messages.SCAFFOLDS`` key) + ``context`` (plain
    JSON-safe scalars) so the sentence re-renders in each recipient's locale; ``body`` stays the
    frozen English audit/fallback column and ``title`` may itself carry ``{slot}`` placeholders
    (it is rendered against ``context`` under the broadcast locale). A site that has not migrated
    passes ``body`` alone and keeps delivering verbatim English, exactly as before.

    Thin wrapper over :func:`emit_alert` that defaults ``channels`` to
    :func:`enabled_channel_values` (in-app + EVE-mail + Discord + Telegram + WhatsApp +
    Slack), so a service reaches every channel the corp armed without re-deriving the list.
    The **audience follows the category's routing** (``None`` here → e.g. ``home_defence`` is
    corp-wide, ``structure_timer``/``industry_job`` officer-only, ``logistics`` user-scoped);
    a caller that genuinely wants corp-wide delivery (a fleet announcement) passes
    ``audience={"kind": "corp"}`` explicitly.

    Always ``source="service"`` — it skips the interactive manual-confirmation gates
    (two-step / large-audience ack) and, because service alerts are idempotent + dedup-
    guarded, bypasses the manual dispatch rate limits (matching calendar reminders) so an
    automatic emergency alert is never silently dropped by the officer urgent/day budget.
    Keeps duplicate suppression + audit + history. Best-effort: returns the ``Alert`` or
    ``None`` when suppressed.
    """
    return emit_alert(
        category=category, title=title, body=body, priority=priority,
        template=template, context=context,
        audience=audience,
        channels=channels if channels is not None else enabled_channel_values(),
        source=AlertSource.SERVICE, source_service=source_service,
        source_object_id=source_object_id, reason=reason,
        idempotency_key=idempotency_key, created_by=created_by,
        bypass_ratelimit=True,
    )


# --- helpers -----------------------------------------------------------------
def recipient_estimate(audience: dict | None) -> int:
    return RecipientResolver().estimate(audience)


def dispatch_requirements(category: str, priority: str, audience: dict | None) -> dict:
    """What confirmations a manual dispatch of this alert needs — for the composer UI
    (and enforced in ``emit_alert``). All values come from the ``anti_abuse`` config."""
    aa = config.get("anti_abuse")
    est = recipient_estimate(audience)
    return {
        "estimated_recipients": est,
        "needs_reason": priority in _URGENT and bool(aa.get("require_reason_for_urgent", True)),
        "needs_two_step": priority in _URGENT and bool(aa.get("two_step_urgent", True)),
        "needs_large_audience_ack": est > aa.get("large_audience_threshold", 50),
        "needs_approval": category in (aa.get("approval_required_categories") or []),
    }


def _enqueue(alert_id: int) -> None:
    from .tasks import deliver_alert

    transaction.on_commit(lambda: deliver_alert.delay(alert_id))


def _template_obj(template):
    if isinstance(template, AlertTemplate):
        return template
    if isinstance(template, str) and template:
        return AlertTemplate.objects.filter(key=template, enabled=True).first()
    return None


def _resolve_template_key(template) -> str:
    """The stable message-identity string to persist on ``Alert.template_key``.

    Covers a code message-scaffold key, a DB ``AlertTemplate.key``, or ``""``. Kept as a
    plain string (separate from the ``template`` FK) so a historical alert stays
    re-renderable/auditable even if a DB template is later deleted (doc 08 §4.1).
    """
    if isinstance(template, AlertTemplate):
        return template.key
    if isinstance(template, str):
        return template
    return ""


def _persist_context(context) -> dict:
    """The audit-safe ``{str: str}`` context to persist on ``Alert.context``.

    Reuses ``rendering._flatten`` (every value coerced to ``str``) so the stored map is
    always JSON-serialisable and carries exactly the interpolation values the frozen,
    audit-safe ``Alert.body`` already reflects (no secrets/tokens — doc 08 §4.4).
    """
    return rendering._flatten(context)


def _render_title(title, context) -> str:
    """The frozen (broadcast-locale) audit title.

    A migrated service passes an English title *template* carrying ``{slot}`` placeholders, so it
    is rendered against the same raw context as the body. A title that is already interpolated
    (a legacy f-string) may still contain a stray brace from an EVE/user name — that is a bad
    template, never a reason to lose the alert, so it degrades to the raw string.
    """
    if not context:
        return title
    try:
        return rendering.render(title, context)
    except rendering.TemplateError:
        return title


def _render_body(template, body, context) -> tuple[str, bool]:
    """Return (rendered_body, is_custom_message).

    A code message-scaffold key (``messages.SCAFFOLDS``) and a DB ``AlertTemplate`` both
    render a template-backed body (``custom=False``); only a bare ``body=`` with no key
    yields ``custom=True`` (genuine verbatim free-text). The scaffold is checked first so
    a code registry wins over a same-named DB template, matching ``render_for``.
    """
    if isinstance(template, str) and template:
        from .messages import SCAFFOLDS

        sc = SCAFFOLDS.get(template)
        if sc is not None:
            # str() resolves the gettext_lazy proxy under the active override(broadcast).
            return rendering.render(str(sc.body), context), False
    tpl = _template_obj(template)
    if tpl is not None:
        missing = rendering.missing_required(tpl.required_vars, context)
        if missing:
            # Lazy: _render_body runs under translation.override(broadcast_locale()), but the
            # officer reads this via str(exc) in views after the override exits.
            raise ValueError(
                _lazy("Missing required template variables: %(variables)s")
                % {"variables": ", ".join(missing)}
            )
        return rendering.render(tpl.body, context), False
    if body is None:
        return "", True
    return (rendering.render(body, context) if context else body), True


_SEVERITY = {
    AlertPriority.LOW: 10,
    AlertPriority.NORMAL: 30,
    AlertPriority.HIGH: 60,
    AlertPriority.URGENT: 85,
    AlertPriority.EMERGENCY: 100,
}


def _severity_for(priority: str) -> int:
    return _SEVERITY.get(priority, 30)
