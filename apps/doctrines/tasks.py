"""Doctrine housekeeping beat: prune staged XML-import batches (roadmap 0.13)."""
from __future__ import annotations

from celery import shared_task


@shared_task(name="doctrines.housekeeping")
def housekeeping() -> dict:
    """Prune ``DoctrineImportBatch`` staging rows the model docstring promises to.

    A batch is disposable staging whose parsed ``payload`` can be large. Abandoned
    PREVIEW batches (staged but never committed) are pruned after a short TTL;
    terminal (committed/expired) batches are kept a while as import history, then
    pruned. Convergent (age-based), so a missed night is caught up the next run.
    """
    import datetime as dt

    from django.utils import timezone

    from .models import DoctrineImportBatch

    now = timezone.now()
    counts: dict[str, int] = {}
    # Abandoned previews: staged but never committed within a short window.
    counts["abandoned_previews"] = DoctrineImportBatch.objects.filter(
        status=DoctrineImportBatch.Status.PREVIEW, created_at__lt=now - dt.timedelta(days=2)
    ).delete()[0]
    # Terminal batches kept as import history for a month, then pruned.
    counts["old_terminal"] = DoctrineImportBatch.objects.filter(
        status__in=[DoctrineImportBatch.Status.COMMITTED, DoctrineImportBatch.Status.EXPIRED],
        created_at__lt=now - dt.timedelta(days=30),
    ).delete()[0]
    return counts
