"""Readiness alert evaluation + delivery (design doc 13).

Turns ``readiness.alerts`` rules into deduped, escalating notifications over the
latest ``ReadinessFinding`` state. Reuses the app's existing Discord primitive
(``broadcast_discord``) — it never invents a delivery mechanism — and every send is
best-effort (a dead webhook degrades to a no-op, never blocking the alert record).
Ships **inert**: with no rules configured (the shipped default) nothing fires.

Matching (doc 13 §1.3) is over findings, which carry the severity/kind/dimension/kpi
the rules key on. Raw-value/score conditions (`score_below` etc.) become live in a
later phase when per-KPI scores are persisted on findings; the structural matches
(`status_is`/`kind`/scoping by dimension/kpi) work today.
"""
from __future__ import annotations

import logging

from django.utils.translation import gettext as _

logger = logging.getLogger(__name__)

_SEVERITY_PREFIX = {"info": "ℹ️", "warn": "⚠️", "high": "🟧", "critical": "🟥"}
_OWNER_NOTIFY_SEVERITIES = {"high", "critical"}


def _drill_link(finding) -> str:
    """An on-site drill-down path for a finding (never an external URL)."""
    if finding is None or not finding.dimension_key:
        return "/readiness/"
    return f"/readiness/d/{finding.dimension_key}/"


def _owner_label(owner_tag: str, responsibilities: dict) -> str:
    if not owner_tag:
        return _("Unassigned")
    return ((responsibilities.get("owner_tags") or {}).get(owner_tag) or {}).get("label", owner_tag)


def render_alert(rule: dict, finding, responsibilities: dict) -> str:
    """The Discord/plain-text message for an alert (markdown doubles as Discord)."""
    severity = rule.get("severity", "warn")
    prefix = _SEVERITY_PREFIX.get(severity, "⚠️")
    label = finding.dimension_key if finding else rule.get("key", "readiness")
    if finding and finding.kpi_key:
        label = f"{finding.dimension_key}/{finding.kpi_key}"
    # ``title_i18n`` resolves the finding's scaffold under the ACTIVE locale (the dispatcher's
    # ``translation.override`` for a per-recipient send); a keyless/legacy finding returns its
    # stored English verbatim.
    summary = finding.title_i18n if finding else rule.get("key", "")
    owner = _owner_label(getattr(finding, "owner_tag", ""), responsibilities)
    link = _drill_link(finding)
    bits = [f"{prefix} **{severity.upper()} — {label}** {summary}."]
    if severity in _OWNER_NOTIFY_SEVERITIES:
        bits.append(_("Owner: %(owner)s.") % {"owner": owner})
    bits.append(f"<{link}>")
    return " ".join(bits)


def _matches(rule: dict, finding) -> bool:
    """Whether a finding satisfies a rule's ``match`` (structural conditions only)."""
    match = rule.get("match") or {}
    if "dimension" in match and match["dimension"] != finding.dimension_key:
        return False
    if "kpi" in match and match["kpi"] != finding.kpi_key:
        return False
    if "kind" in match and match["kind"] != finding.kind:
        return False
    # Score-precise matching: a finding carries the score of the KPI it represents
    # (``None`` when it maps to none). A score condition only matches a scored finding.
    score_below = match.get("score_below")
    if score_below is not None and (finding.score is None or finding.score >= score_below):
        return False
    score_above = match.get("score_above")
    if score_above is not None and (finding.score is None or finding.score <= score_above):
        return False
    when = match.get("when")
    if when == "status_is":
        # Findings don't carry a green/amber/red status; an open finding is, by
        # definition, a non-green state, so a red/amber match is satisfied by its
        # presence.
        return match.get("value") in ("red", "amber", None)
    return True


def _site_base() -> str:
    """The site origin for in-mail links (from CSRF_TRUSTED_ORIGINS), or ''."""
    from django.conf import settings

    origins = getattr(settings, "CSRF_TRUSTED_ORIGINS", []) or []
    return origins[0].rstrip("/") if origins else ""


def render_mail(rule: dict, finding, responsibilities: dict) -> tuple[str, str]:
    """The EVE-mail (subject, body) for an alert (doc 13 §EVE-mail template)."""
    severity = rule.get("severity", "warn")
    label = finding.dimension_key if finding else rule.get("key", "readiness")
    if finding and finding.kpi_key:
        label = f"{finding.dimension_key}/{finding.kpi_key}"
    owner = _owner_label(getattr(finding, "owner_tag", ""), responsibilities)
    link = f"{_site_base()}{_drill_link(finding)}"
    subject = f"[FORCA Readiness] {severity.upper()}: {label}"
    score = getattr(finding, "score", None)
    score_line = (_("Current score: %(score)s") % {"score": score}) + "\n" if score is not None else ""
    owner_line = _("Owner: %(owner)s") % {"owner": owner}
    detail_line = _("Detail / action: %(link)s") % {"link": link}
    footer = _("— FORCA Command Grid (automated). Manage alert rules in the Admin Console.")
    body = (
        f"{finding.title_i18n if finding else ''}\n"
        f"{score_line}"
        f"{owner_line}\n"
        f"{detail_line}\n"
        f"{footer}"
    )
    return subject, body


def _deliver(message: str, channels, *, rule=None, finding=None, responsibilities=None) -> list:
    """Best-effort fan-out. Returns the channels actually delivered (doc 13 §0)."""
    delivered = []
    if "discord" in channels:
        try:
            from apps.pingboard import notifications
            from apps.recommendations.notify import broadcast_discord

            # Readiness alerts are leadership content: broadcast only when the event is
            # enabled, and stamped with its audience classification so it reaches only
            # leadership-cleared chat channels, never a mass corp channel.
            policy = notifications.resolve("readiness.alert")
            if policy["enabled"] and broadcast_discord(message, classification=policy["classification"]):
                delivered.append("discord")
        except Exception:  # noqa: BLE001 - delivery must never block the alert record
            logger.exception("readiness alert: discord delivery failed")
    # EVE-mail outbound: only for owner-notify severities (high/critical) with a mapped
    # owner (doc 13 §EVE-mail). Sends in-game from the configured director-sender; a
    # missing sender/token/recipient degrades to a no-op (never blocks the record).
    if (
        "eve_mail" in channels and finding is not None and rule is not None
        and rule.get("severity") in _OWNER_NOTIFY_SEVERITIES
    ):
        try:
            from .mail import owner_recipient_ids, send_mail

            recipients = owner_recipient_ids(getattr(finding, "owner_tag", ""), responsibilities or {})
            subject, body = render_mail(rule, finding, responsibilities or {})
            if send_mail(subject, body, recipients):
                delivered.append("eve_mail")
        except Exception:  # noqa: BLE001 - delivery must never block the alert record
            logger.exception("readiness alert: eve-mail delivery failed")
    return delivered


def evaluate_alerts(now=None) -> int:
    """Evaluate alert rules over current findings: fire, escalate, resolve. Idempotent.

    Returns the number of fresh alerts fired this run. With no rules configured this
    is a no-op. Cooldown dedupes re-delivery; ``escalated_at``/``resolved_at`` guards
    make a repeated run over unchanged state a no-op.
    """
    import datetime as dt

    from django.utils import timezone

    from . import config as config_module
    from .models import ReadinessAlert, ReadinessFinding

    now = now or timezone.now()
    rules = config_module.get("alerts").get("rules", [])
    responsibilities = config_module.get("responsibilities")
    # An ACKNOWLEDGED finding is still-broken (an officer is working it), NOT recovered
    # — so it keeps its alert matched. Only a truly RESOLVED/cleared finding lets the
    # resolution sweep close the alert.
    open_findings = list(
        ReadinessFinding.objects.filter(
            status__in=[ReadinessFinding.Status.OPEN, ReadinessFinding.Status.ACKNOWLEDGED]
        )
    )

    fired = 0
    matched_alert_ids: set[int] = set()
    for rule in rules:
        key = rule.get("key")
        if not key:
            continue
        cooldown_h = int(rule.get("cooldown_hours", 24) or 0)
        channels = rule.get("channels", [])
        escalate_after = rule.get("escalate_after_hours")
        escalate_channels = rule.get("escalate_channels", [])

        for finding in open_findings:
            if not _matches(rule, finding):
                continue
            open_alert = (
                ReadinessAlert.objects.filter(
                    rule_key=key, dimension_key=finding.dimension_key,
                    kpi_key=finding.kpi_key, resolved_at__isnull=True,
                ).order_by("-created_at").first()
            )
            if open_alert is not None:
                matched_alert_ids.add(open_alert.id)
                # Escalation: an open alert older than the window, not yet escalated.
                if (
                    escalate_after and open_alert.escalated_at is None
                    and open_alert.created_at <= now - dt.timedelta(hours=int(escalate_after))
                ):
                    delivered = _deliver(render_alert(rule, finding, responsibilities), escalate_channels,
                                         rule=rule, finding=finding, responsibilities=responsibilities)
                    # Only consume the one-shot escalation if it actually notified —
                    # a misconfigured (empty/dead) escalate channel shouldn't silently
                    # burn it; retry next tick.
                    if delivered:
                        open_alert.escalated_at = now
                        open_alert.save(update_fields=["escalated_at"])
                # Cooldown: within the window → no re-delivery.
                continue

            # Cooldown after resolution: if the same key resolved within the cooldown
            # window, a re-fire is suppressed so a flapping finding doesn't re-alert
            # every cycle (doc 13 §4). The finding stays on the risk register either way.
            if cooldown_h:
                recently_resolved = ReadinessAlert.objects.filter(
                    rule_key=key, dimension_key=finding.dimension_key, kpi_key=finding.kpi_key,
                    resolved_at__gte=now - dt.timedelta(hours=cooldown_h),
                ).exists()
                if recently_resolved:
                    continue

            # Fresh fire: record the alert first, then deliver (best-effort).
            message = render_alert(rule, finding, responsibilities)
            # Seam B: the summary is a frozen copy of the finding's English title. Carry the
            # finding's scaffold key + params across so the alert log re-renders under each
            # reader's locale; a finding with no key yields no key here and the alert keeps
            # rendering its stored English.
            alert = ReadinessAlert.objects.create(
                rule_key=key, dimension_key=finding.dimension_key, kpi_key=finding.kpi_key,
                severity=rule.get("severity", "warn"), summary=finding.title[:300],
                summary_key=finding.title_key, summary_params=finding.title_params or {},
                finding=finding,
            )
            alert.channels = _deliver(message, channels,
                                      rule=rule, finding=finding, responsibilities=responsibilities)
            alert.save(update_fields=["channels"])
            matched_alert_ids.add(alert.id)
            fired += 1

    # Resolution sweep: any open alert whose rule no longer matches has recovered.
    for alert in ReadinessAlert.objects.filter(resolved_at__isnull=True):
        if alert.id not in matched_alert_ids:
            alert.resolved_at = now
            alert.save(update_fields=["resolved_at"])

    return fired
