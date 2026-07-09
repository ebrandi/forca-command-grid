"""Best-effort notification + calendar seams for the raffle.

Thin wrappers over Pingboard's public API. Every call is wrapped so a notification
problem can never block a raffle business action (draw, grant, fulfilment). All are
inert until leadership arms channels at /ops/admin/pingboard/.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("forca.raffle")


def _site_base() -> str:
    from django.conf import settings

    base = getattr(settings, "FORCA_SITE_URL", "") or getattr(settings, "SITE_URL", "")
    return base.rstrip("/")


def contest_url(contest) -> str:
    return f"{_site_base()}/raffle/{contest.slug}/"


def announce(contest, *, title: str, body: str, suffix: str, audience: dict | None = None):
    """Corp-wide (or targeted) multi-channel announcement — returns Alert or None."""
    try:
        from apps.pingboard import services as pingboard

        return pingboard.emit_broadcast(
            category="announcement",
            title=title,
            body=body,
            audience=audience or {"kind": "corp"},
            source_service="raffle",
            source_object_id=f"{suffix}:{contest.id}",
            idempotency_key=f"raffle:{suffix}:{contest.id}",
        )
    except Exception:  # noqa: BLE001 — never let notification break the action
        logger.exception("raffle announce failed (%s)", suffix)
        return None


def notify_user(contest, user_id: int, *, title: str, body: str, suffix: str):
    """DM a single pilot (winner / ticket / eligibility nudge)."""
    if not user_id:
        return None
    return announce(
        contest, title=title, body=body, suffix=f"{suffix}:{user_id}",
        audience={"kind": "user", "id": user_id},
    )


def publish_timeline(contest):
    """Idempotently put the draw on the corp calendar (best-effort)."""
    try:
        from apps.pingboard import calendar
        from apps.pingboard.models import CalendarEventStatus, CalendarEventType

        calendar.publish_event(
            source_system="raffle",
            source_object_id=f"contest:{contest.id}",
            event_type=CalendarEventType.CUSTOM,
            title=f"Raffle draw — {contest.name}",
            start_at=contest.draw_at,
            end_at=None,
            status=CalendarEventStatus.SYNCED,
            visibility="member",
        )
    except Exception:  # noqa: BLE001
        logger.exception("raffle calendar publish failed")


def cancel_timeline(contest):
    try:
        from apps.pingboard import calendar

        calendar.cancel_event(source_system="raffle", source_object_id=f"contest:{contest.id}")
    except Exception:  # noqa: BLE001
        logger.exception("raffle calendar cancel failed")
