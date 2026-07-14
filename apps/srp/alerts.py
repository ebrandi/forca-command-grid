"""SRP-2 (roadmap 2.7) — SRP SLA & solvency alerts.

The readiness ``srp`` dimension already grades the ship-replacement queue against
leadership's configured bounds (backlog size, average approval wait, oldest open
claim) and the period budget — but nothing *tells* anyone when a bound is breached;
a director only learns the SRP queue has gone sideways if they open the page.

This fires **one deduped digest** to SRP officers when any SLA/solvency bound
breaches, and resets when the queue is back within SLA — reusing the same
thresholds the readiness score uses (no new config) and the shared
``pingboard.dedup`` machinery (one alert per distinct breach set, retries on a
suppressed emit). One digest, never per-claim.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.utils import timezone

_EVENT_KEY = "srp.sla_alert"
_SIG_KEY = "srp_alert:sig"


def _isk(amount) -> str:
    """Compact ISK for an alert line (2.3B / 940.0M / 12,500 ISK)."""
    v = float(amount or 0)
    if abs(v) >= 1e9:
        return f"{v / 1e9:.1f}B ISK"
    if abs(v) >= 1e6:
        return f"{v / 1e6:.1f}M ISK"
    return f"{v:,.0f} ISK"


def srp_breaches() -> list[dict]:
    """Current SLA/solvency breaches vs the readiness ``srp`` thresholds.

    Returns ``[]`` when the corp runs no active SRP programme (nothing to grade) or
    every bound is within SLA. Each breach is ``{"key", "detail"}`` — ``key`` feeds
    the dedup signature, ``detail`` is the human digest line.
    """
    from apps.readiness import config as rconfig
    from apps.srp.models import SrpBudget, SrpClaim, SrpProgram

    if not SrpProgram.objects.filter(is_active=True).exists():
        return []

    cfg = rconfig.get("srp")
    max_pending = int(cfg["max_pending_claims"]) or 1
    max_wait = int(cfg["max_avg_wait_hours"]) or 1
    max_age = int(cfg["max_claim_age_days"]) or 1
    now = timezone.now()
    breaches: list[dict] = []

    # Backlog + oldest open claim — an open claim is submitted (awaiting a decision)
    # or approved-but-unpaid (awaiting payout); both are an unsettled obligation.
    open_statuses = [SrpClaim.Status.SUBMITTED, SrpClaim.Status.APPROVED]
    created = list(
        SrpClaim.objects.filter(status__in=open_statuses).values_list("created_at", flat=True)
    )
    backlog = len(created)
    if backlog > max_pending:
        breaches.append({
            "key": "backlog",
            "detail": f"{backlog} SRP claims are pending a decision or payout (SLA max {max_pending}).",
        })
    oldest_age = max(((now - c).days for c in created), default=0)
    if oldest_age > max_age:
        breaches.append({
            "key": "oldest",
            "detail": f"The oldest open SRP claim is {oldest_age} days old (SLA max {max_age}).",
        })

    # Average approval wait over the last 30 days.
    since = now - dt.timedelta(days=30)
    decided = list(
        SrpClaim.objects.filter(decided_at__isnull=False, decided_at__gte=since)
        .values_list("created_at", "decided_at")
    )
    if decided:
        avg_wait = sum((d - c).total_seconds() for c, d in decided) / len(decided) / 3600.0
        if avg_wait > max_wait:
            breaches.append({
                "key": "wait",
                "detail": f"Average SRP approval wait is {avg_wait:.0f}h over the last 30d "
                          f"(SLA max {max_wait}h).",
            })

    # Solvency — this period's paid + still-owed obligation vs the allocated budget.
    # Only graded when a budget is allocated for the period (mirrors the readiness
    # dimension, which skips budget health when there is no allocation to grade against).
    period = now.strftime("%Y-%m")
    budget = SrpBudget.objects.filter(period=period).first()
    if budget and budget.allocated:
        from apps.srp.services import exposure, spent_for_period

        spent = spent_for_period(period)
        owed = exposure()  # open liability (submitted + approved-unpaid)
        if spent + owed > Decimal(budget.allocated):
            breaches.append({
                "key": "solvency",
                "detail": f"SRP is over budget for {period}: {_isk(spent)} paid + {_isk(owed)} "
                          f"pending exceeds the {_isk(budget.allocated)} allocation.",
            })

    return breaches


def scan_srp_health() -> dict:
    """Fire one deduped SRP-officer digest when the SLA/solvency breach set changes."""
    from apps.pingboard.dedup import fire_on_change

    breaches = srp_breaches()
    body = ""
    lines = ""
    if breaches:
        lines = "\n".join(f"• {b['detail']}" for b in breaches)
        body = (
            "SRP is breaching its service level:\n\n" + lines +
            "\n\nReview the SRP queue and clear/decide the oldest claims. This digest "
            "fires once per distinct breach set and resets when SRP is back within SLA."
        )
    return fire_on_change(
        event_key=_EVENT_KEY, sig_key=_SIG_KEY,
        problems=[b["key"] for b in breaches],
        title="SRP needs attention", body=body,
        # The digest chrome localises per SRP officer; the breach lines are diagnostic data and
        # stay raw. ``body`` remains the frozen English audit column.
        template="srp.sla_breach", context={"details": lines},
        source_service="srp", source_prefix="srp_sla",
    )
