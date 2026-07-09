"""Raffle background jobs (Celery). Thin wrappers that lazy-import the service so
autodiscovery is cheap; every task is idempotent and safe to retry.

Registered in ``config/celery.py`` beat_schedule. Verify with
``celery -A config inspect registered`` against the live worker (a bare
``python -c`` won't run django.setup()).
"""
from __future__ import annotations

from celery import shared_task


@shared_task(name="raffle.lifecycle")
def lifecycle() -> dict:
    """Open scheduled contests whose start passed; close active ones that ended."""
    from . import services

    return {"opened": services.open_scheduled_contests(),
            "closed": services.close_ended_contests()}


@shared_task(name="raffle.process_sources")
def process_sources() -> int:
    """Sweep every enabled source for every active contest into the ledger."""
    from .engine import process_all_sources
    from .models import RaffleContest

    processed = 0
    for contest in RaffleContest.objects.filter(status=RaffleContest.Status.ACTIVE):
        process_all_sources(contest)
        processed += 1
    return processed


@shared_task(name="raffle.recompute_summaries")
def recompute_summaries() -> int:
    """Rebuild leaderboard summaries for active + closed contests."""
    from . import services
    from .models import RaffleContest

    n = 0
    for contest in RaffleContest.objects.filter(
        status__in=[RaffleContest.Status.ACTIVE, RaffleContest.Status.CLOSED]
    ):
        services.recompute_summaries(contest)
        n += 1
    return n


@shared_task(name="raffle.draw_due")
def draw_due() -> int:
    """Execute the automatic draw for closed contests whose draw time has passed.

    A due-table sweep + the run_draw cross-worker lock make this safe under
    duplicate/missed beats (the pingboard-dispatch idiom).
    """
    from django.utils import timezone

    from . import services
    from .models import RaffleContest

    drawn = 0
    due = RaffleContest.objects.filter(
        status=RaffleContest.Status.CLOSED, auto_draw=True, draw_at__lte=timezone.now()
    )
    for contest in due:
        try:
            result = services.run_draw(contest)
            if result is not None:
                drawn += 1
        except services.ActivityNotMet:
            # Expected: minimum activity not reached — hold, don't auto-draw. A
            # Director can still draw manually with the override.
            continue
        except Exception:  # noqa: BLE001 — one bad contest must not stall the sweep
            import logging
            logging.getLogger("forca.raffle").exception("auto-draw failed for %s", contest.pk)
    return drawn


@shared_task(name="raffle.integrity_scan")
def integrity_scan() -> int:
    """Flag suspicious ticket events for officer review across live contests."""
    from . import integrity
    from .models import RaffleContest

    flags = 0
    for contest in RaffleContest.objects.filter(
        status__in=[RaffleContest.Status.ACTIVE, RaffleContest.Status.CLOSED]
    ):
        flags += integrity.scan_contest(contest)
    return flags


@shared_task(name="raffle.refresh_adoption")
def refresh_adoption() -> int:
    """Warm the ESI-adoption metric caches (global + per live contest)."""
    from . import stats
    from .models import RaffleContest

    stats.adoption_metrics(use_cache=False)
    n = 0
    for contest in RaffleContest.objects.filter(
        status__in=[RaffleContest.Status.ACTIVE, RaffleContest.Status.CLOSED,
                    RaffleContest.Status.COMPLETED]
    ):
        stats.contest_statistics(contest, use_cache=False)
        n += 1
    return n
