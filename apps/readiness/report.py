"""Weekly executive report (design doc 13 §6).

Mirrors ``pilots.deliver_leadership_briefing``: the in-app readiness state is the
source of truth; this builds the week's summary, archives it as an ``ExecutiveReport``
(idempotent on the period), and ships a plain-text digest to Discord + email
best-effort. No new data, no ESI.
"""
from __future__ import annotations

import logging

from django.conf import settings

log = logging.getLogger(__name__)


def _movers(period_start) -> list[dict]:
    """Week-over-week dimension deltas: the latest snapshot vs the one ~a week back."""
    from .models import ReadinessSnapshot

    latest = ReadinessSnapshot.objects.order_by("-created_at").first()
    prior = (
        ReadinessSnapshot.objects.filter(created_at__lt=period_start)
        .order_by("-created_at").first()
    )
    if latest is None:
        return []
    prior_dims = (prior.dimensions if prior else {}) or {}
    out = []
    for key, score in (latest.dimensions or {}).items():
        if score is None:
            continue
        was = prior_dims.get(key)
        delta = (score - was) if isinstance(was, int) else 0
        out.append({"dimension": key, "score": score, "delta": delta})
    out.sort(key=lambda d: d["delta"])
    return out


def build_report(period_start, period_end) -> dict:
    """Compose the report body: index, movers, top risks, top tasks."""
    from apps.tasks.models import Task

    from .models import ReadinessFinding, ReadinessSnapshot

    latest = ReadinessSnapshot.objects.order_by("-created_at").first()
    index = latest.index if latest else 0
    movers = _movers(period_start)
    risks = [
        {"title": f.title, "dimension": f.dimension_key, "severity": f.severity, "owner": f.owner_tag}
        for f in ReadinessFinding.objects.filter(status=ReadinessFinding.Status.OPEN)
        .order_by("-weight")[:5]
    ]
    tasks = list(
        Task.objects.filter(related_type="readiness")
        .exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])
        .order_by("-priority")
        .values_list("title", flat=True)[:5]
    )
    return {
        "index": index,
        "movers": movers,
        "top_risks": risks,
        "top_tasks": list(tasks),
        # movers is sorted by delta asc, so [-1] is the biggest gain, [0] the biggest
        # drop — each shown only when it's genuinely a gain / a drop.
        "best": movers[-1] if movers and movers[-1]["delta"] > 0 else None,
        "worst": movers[0] if movers and movers[0]["delta"] < 0 else None,
    }


def format_digest(body: dict, period_start, period_end) -> str:
    """Render the report body as a compact plain-text/Discord-markdown digest."""
    corp = getattr(settings, "FORCA_CORP_NAME", "Corp")
    lines = [
        f"**{corp} — weekly readiness report** ({period_start:%d %b}–{period_end:%d %b})",
        f"• Overall index: {body['index']}%",
    ]
    if body.get("worst"):
        w = body["worst"]
        lines.append(f"• Biggest drop: {w['dimension']} {w['delta']:+d} (now {w['score']})")
    if body.get("best"):
        b = body["best"]
        lines.append(f"• Biggest gain: {b['dimension']} {b['delta']:+d} (now {b['score']})")
    risks = body.get("top_risks") or []
    if risks:
        lines.append("• Top risks: " + "; ".join(r["title"] for r in risks[:3]))
    tasks = body.get("top_tasks") or []
    if tasks:
        lines.append(f"• Open readiness tasks: {len(tasks)} (top: {tasks[0]})")
    lines.append("Full board: </readiness/>")
    return "\n".join(lines)


def weekly_report(period_start=None, period_end=None) -> dict:
    """Build, archive (idempotent per period), and deliver the weekly report."""
    import datetime as dt

    from django.utils import timezone

    from .models import ExecutiveReport

    today = (period_end or timezone.now().date())
    end = today
    start = period_start or (end - dt.timedelta(days=7))

    body = build_report(timezone.make_aware(dt.datetime.combine(start, dt.time.min)), end)
    report, created = ExecutiveReport.objects.update_or_create(
        period_start=start, period_end=end,
        defaults={"index": body["index"], "body": body},
    )

    # Deliver only once per period: the record updates in place on a re-run/retry, but
    # a digest that already went out this week is not re-broadcast. We gate on whether
    # anything ACTUALLY delivered (a sum > 0), so a report first built while channels
    # were unconfigured (all-zero) can still deliver on a later re-run once they exist.
    if not created and any((report.delivered_channels or {}).values()):
        return report.delivered_channels

    digest = format_digest(body, start, end)
    delivered = {"discord": 0, "email": 0}
    try:
        from apps.pingboard import notifications
        from apps.recommendations.notify import broadcast_discord

        # The weekly executive report is leadership content — governed by the
        # readiness.weekly_report event and classification-gated to leadership channels.
        policy = notifications.resolve("readiness.weekly_report")
        if policy["enabled"]:
            delivered["discord"] = broadcast_discord(digest, classification=policy["classification"])
    except Exception:  # noqa: BLE001 - delivery must never break the beat
        log.exception("weekly report: discord delivery failed")

    recipients = [a for a in getattr(settings, "FORCA_BRIEFING_EMAILS", []) if a]
    if recipients:
        try:
            from django.core.mail import send_mail

            corp = getattr(settings, "FORCA_CORP_NAME", "Corp")
            delivered["email"] = send_mail(
                subject=f"{corp} — weekly readiness report",
                message=digest.replace("**", "").replace("</readiness/>", "/readiness/"),
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                recipient_list=recipients, fail_silently=True,
            )
        except Exception:  # noqa: BLE001
            log.exception("weekly report: email delivery failed")

    report.delivered_channels = delivered
    report.save(update_fields=["delivered_channels"])
    return delivered
