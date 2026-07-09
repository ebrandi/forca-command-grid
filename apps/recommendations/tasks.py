"""Celery task: run the recommendation engine and dispatch alerts."""
from __future__ import annotations

from celery import shared_task

from . import engine
from .notify import dispatch_alerts
from .services import build_action_queue


@shared_task(name="recommendations.run")
def run_recommendations() -> dict:
    created = engine.run_all()
    queued = build_action_queue()
    alerted = dispatch_alerts()
    return {"recommendations": created, "queued": queued, "alerts": alerted}


@shared_task(name="recommendations.sync_notifications")
def sync_notifications() -> dict:
    """Relay interesting in-game ESI notifications to the site and Discord."""
    from .notifications import sync_corp_notifications

    return sync_corp_notifications()


@shared_task(name="recommendations.relay_mail")
def relay_mail() -> dict:
    """Relay new corp/alliance mailing-list mail to Discord. No-op until granted."""
    from .mail_relay import sync_corp_mail

    return sync_corp_mail()


@shared_task(name="recommendations.housekeeping")
def housekeeping() -> dict:
    """Retention prune for relayed defensive-alert history (roadmap 0.13).

    CorpNotification (relayed ESI notifications) and RelayedMail (mailing-list
    headers only — never bodies) accumulate as they are relayed. Keep a bounded
    90-day window, then prune by age. Convergent; a missed night self-heals.
    """
    import datetime as dt

    from django.utils import timezone

    from .models import CorpNotification, RelayedMail

    now = timezone.now()
    cutoff = now - dt.timedelta(days=90)
    counts: dict[str, int] = {}
    counts["notifications"] = CorpNotification.objects.filter(timestamp__lt=cutoff).delete()[0]
    counts["mail"] = RelayedMail.objects.filter(sent_at__lt=cutoff).delete()[0]
    return counts
