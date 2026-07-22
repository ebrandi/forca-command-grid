"""KB-38 — the one-click "import our corp's full history" runner (WS-D5).

The setup wizard's history-import launcher enqueues :func:`run_import`, which drives the
**existing** import management commands — it deliberately re-implements none of their
logic:

  * EVE Ref (``import_everef_killmails``) is the recommended path: it is bound by download
    speed, not the ESI rate limit. We drive it in month-sized date chunks so the wizard's
    progress fragment advances and a cancel can take effect between chunks (the running
    chunk always finishes first).
  * zKillboard (``import_zkill_history``) walks the corp's whole history in one paced pass
    (it re-discovers pages per run, so chunking it would multiply the zKill load); cancel
    for it is therefore only honoured before the single pass starts.

Progress/counts are derived from the ``Killmail`` row count, not by parsing command output:
both importers are idempotent, home-corp-scoped inserts, so the count delta *is* the number
of killmails ingested, for either backend.
"""
from __future__ import annotations

import datetime as dt
import io
import logging

from django.core.management import call_command
from django.utils import timezone

from .models import KillboardHistoryImport, Killmail

log = logging.getLogger("forca.killboard")

# Month-ish window the EVE Ref backfill is driven in, so progress advances and a cancel is
# honoured between chunks without re-implementing the command's own day loop.
_EVEREF_CHUNK_DAYS = 30


def _everef_chunks(start: dt.date, end: dt.date):
    """Yield ``(from, to)`` inclusive date windows covering ``start..end``."""
    day = start
    while day <= end:
        chunk_end = min(day + dt.timedelta(days=_EVEREF_CHUNK_DAYS - 1), end)
        yield day, chunk_end
        day = chunk_end + dt.timedelta(days=1)


def _cancel_requested(job: KillboardHistoryImport) -> bool:
    """Re-read only the cancel flag (a member may set it mid-run from the wizard)."""
    return KillboardHistoryImport.objects.filter(pk=job.pk, cancel_requested=True).exists()


def _run_everef(job: KillboardHistoryImport, corp: int) -> bool:
    """Drive ``import_everef_killmails`` chunk by chunk. Returns False if cancelled."""
    start = job.from_date or dt.date(2007, 12, 1)
    end = job.to_date or timezone.now().date()
    for chunk_from, chunk_to in _everef_chunks(start, end):
        if _cancel_requested(job):
            return False
        out = io.StringIO()
        call_command(
            "import_everef_killmails",
            from_date=chunk_from.isoformat(),
            to_date=chunk_to.isoformat(),
            corp=corp,
            stdout=out,
            stderr=out,
        )
        _sync_progress(job, out.getvalue())
    return True


def _run_zkill(job: KillboardHistoryImport, corp: int) -> bool:
    """Drive ``import_zkill_history`` in one paced pass. Returns False if cancelled."""
    if _cancel_requested(job):
        return False
    out = io.StringIO()
    call_command("import_zkill_history", corp=corp, stdout=out, stderr=out)
    _sync_progress(job, out.getvalue())
    return True


_BACKENDS = {
    KillboardHistoryImport.Source.EVEREF: _run_everef,
    KillboardHistoryImport.Source.ZKILL: _run_zkill,
}


def _sync_progress(job: KillboardHistoryImport, output: str) -> None:
    """Recompute the ingested delta from the board and append the chunk's output."""
    job.ingested = max(0, Killmail.objects.count() - job.killmails_before)
    if output:
        job.append_log(output)
    job.save(update_fields=["ingested", "log_tail"])


def run_import(import_id: int) -> str:
    """Execute one queued history import to completion (or cancellation/failure).

    Idempotent against double-dispatch: a job not left in an active state is a no-op, so a
    retried Celery delivery never re-runs a finished import. Returns the terminal state.
    """
    job = KillboardHistoryImport.objects.filter(pk=import_id).first()
    if job is None or not job.is_active:
        return job.state if job else "missing"

    from django.conf import settings

    corp = getattr(settings, "FORCA_HOME_CORP_ID", 0)
    job.state = KillboardHistoryImport.State.RUNNING
    job.started_at = timezone.now()
    job.killmails_before = Killmail.objects.count()
    job.save(update_fields=["state", "started_at", "killmails_before"])

    backend = _BACKENDS[job.source]
    try:
        completed = backend(job, corp)
    except Exception as exc:  # noqa: BLE001 — a failed import must not crash the worker
        log.exception("history import #%s failed", import_id)
        job.state = KillboardHistoryImport.State.FAILED
        job.errors += 1
        job.append_log(f"\nimport failed: {exc}\n")
        job.finished_at = timezone.now()
        job.save(update_fields=["state", "errors", "log_tail", "finished_at", "ingested"])
        return job.state

    job.ingested = max(0, Killmail.objects.count() - job.killmails_before)
    job.state = (
        KillboardHistoryImport.State.DONE if completed
        else KillboardHistoryImport.State.CANCELLED
    )
    job.finished_at = timezone.now()
    job.save(update_fields=["state", "ingested", "finished_at", "log_tail"])
    return job.state
