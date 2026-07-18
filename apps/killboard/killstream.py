"""KB-20 — optional real-time killmail ingest from zKillboard's R2Z2 sequence feed.

This is a **supplementary fallback, never a replacement** for the authoritative feeds
(the ESI Director corp feed, the zKillboard query-API poll and the EVE Ref archives),
which always run. It is dark-launched **OFF**; when leadership enables it, a frequent beat
task catches up from a persisted sequence cursor, keeps only killmails that involve the
home corp, and ingests them through the existing idempotent ``ingest_killmail`` path —
bringing recent corp kills to within ~a minute (vs the 15-min primary poll).

Because ingestion is idempotent and the primaries + EVE Ref remain the completeness
backstop, enabling/disabling this, or letting it fall behind, never loses a killmail — it
only changes latency.
"""
from __future__ import annotations

import logging
import time
import uuid

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from core.esi.adapters import r2z2
from core.mixins import Source

from .ingest import ingest_killmail
from .models import IngestSourceHealth, KillstreamState

log = logging.getLogger("forca.killboard")

SOURCE = "killstream"

# Per-run work bound (a spike catches up over subsequent 1-min runs instead of
# overrunning the tick), and a 100ms inter-request pace (10 req/s, safely under the
# R2Z2 15 req/s/IP cap). If the cursor is more than MAX_GAP behind the tip — e.g. after a
# long outage, beyond R2Z2's ~24h ephemeral retention — we skip ahead rather than walk
# millions of sequences; the primaries + EVE Ref backfill that gap.
KILLSTREAM_MAX_PER_RUN = 300
KILLSTREAM_SLEEP_S = 0.1
KILLSTREAM_MAX_GAP = 20000
# Wall-clock budget per run. A run stops after this many seconds regardless of how many
# sequences remain, so it can never outlive the lock TTL — which is the failure mode when
# R2Z2 is *degraded but not timing out* (1-3s/response, exactly what this fallback exists
# for): without a budget, 300 slow fetches could run for minutes, the lock would expire
# mid-run, and the next beat tick would start a second concurrent walker. The next tick
# resumes from the persisted cursor instead.
KILLSTREAM_MAX_RUNTIME_S = 45

_LOCK_KEY = "kb:killstream:lock"
# Comfortably above the per-run wall-clock budget plus one in-flight request timeout, so
# the lock only ever auto-expires when a worker is *killed* mid-run — in which case the
# feed simply pauses at most this long and the primary feeds carry the board meanwhile.
_LOCK_TTL = 300

# The state fields the consumer may persist. NOTE: ``enabled`` is deliberately excluded so
# a run in flight can never clobber an officer toggling the feed off mid-run.
_STATE_FIELDS = [
    "last_sequence", "last_run_at", "last_success_at", "last_error", "last_error_at",
    "last_run_scanned", "last_run_ingested", "ingested_total", "updated_at",
]


def _involves_home(esi: dict, home: int) -> bool:
    """True if the home corp is the victim or any attacker on this killmail body."""
    if (esi.get("victim") or {}).get("corporation_id") == home:
        return True
    return any(a.get("corporation_id") == home for a in (esi.get("attackers") or []))


def _save(state: KillstreamState) -> None:
    state.save(update_fields=_STATE_FIELDS)


def _fail(state: KillstreamState, message: str) -> None:
    now = timezone.now()
    state.last_run_at = now
    state.last_error = str(message)[:300]
    state.last_error_at = now
    _save(state)
    IngestSourceHealth.record(SOURCE, error=message)


def consume_killstream(
    *,
    max_fetch: int = KILLSTREAM_MAX_PER_RUN,
    sleep_s: float = KILLSTREAM_SLEEP_S,
    max_runtime_s: float = KILLSTREAM_MAX_RUNTIME_S,
) -> dict:
    """Catch up the home-corp killstream from the persisted cursor. Returns a summary dict.

    No-op (zero HTTP) unless the fallback is enabled and a home corp is configured. An
    owner-token cache lock serialises runs so two workers never walk the cursor at once, and
    the cursor is (re)loaded inside the lock so a second run can never start from a stale one.
    """
    home = getattr(settings, "FORCA_HOME_CORP_ID", 0)
    # Cheap pre-check evaluated every minute: skip the lock and touch no network when the
    # fallback is off or unconfigured (the common case).
    if not KillstreamState.load().enabled or not home:
        return {"status": "disabled", "scanned": 0, "ingested": 0}

    # Owner-token lock: only the run that set this exact token may clear it, so a run that
    # (somehow) outlives the TTL can never delete a successor's lock.
    token = uuid.uuid4().hex
    if not cache.add(_LOCK_KEY, token, timeout=_LOCK_TTL):
        return {"status": "locked", "scanned": 0, "ingested": 0}
    try:
        state = KillstreamState.load()  # fresh cursor, read INSIDE the lock
        return _run(state, home, max_fetch, sleep_s, max_runtime_s)
    finally:
        if cache.get(_LOCK_KEY) == token:
            cache.delete(_LOCK_KEY)


def _run(state: KillstreamState, home: int, max_fetch: int, sleep_s: float,
         max_runtime_s: float) -> dict:
    state.last_run_at = timezone.now()
    try:
        tip = r2z2.latest_sequence()
    except requests.RequestException as exc:
        _fail(state, f"sequence tip: {exc}")
        return {"status": "error", "error": str(exc), "scanned": 0, "ingested": 0}
    if tip is None:
        _fail(state, "sequence tip missing from R2Z2 payload")
        return {"status": "error", "scanned": 0, "ingested": 0}

    # First run ever: start fresh at the current tip. We never replay the ephemeral window —
    # historical completeness is EVE Ref's job, not the realtime tap's.
    if state.last_sequence is None:
        state.last_sequence = tip
        state.last_success_at = timezone.now()
        state.last_run_scanned = 0
        state.last_run_ingested = 0
        state.last_error = ""
        _save(state)
        IngestSourceHealth.record(SOURCE, count=0)
        return {"status": "initialized", "sequence": tip, "scanned": 0, "ingested": 0}

    start = state.last_sequence + 1
    gap_skipped = 0
    if tip - start > KILLSTREAM_MAX_GAP:
        gap_skipped = (tip - KILLSTREAM_MAX_GAP) - start
        log.warning(
            "killstream behind by %s sequences (> %s); skipping ahead — the primary feeds "
            "and EVE Ref backfill the gap", tip - start, KILLSTREAM_MAX_GAP,
        )
        start = tip - KILLSTREAM_MAX_GAP

    scanned = ingested = fetched = 0
    seq = start
    deadline = time.monotonic() + max_runtime_s
    while seq <= tip and fetched < max_fetch:
        if time.monotonic() >= deadline:
            break  # wall-clock budget hit; the next run resumes from the persisted cursor
        try:
            package = r2z2.fetch_package(seq)
        except requests.RequestException as exc:
            state.last_sequence = seq - 1  # persist progress before bailing
            _fail(state, f"seq {seq}: {exc}")
            return {"status": "error", "error": str(exc), "scanned": scanned, "ingested": ingested}
        fetched += 1
        if package is None:
            # 404. At/after the tip it just means the file isn't published yet — we've
            # caught up; stop and retry this sequence next run (cursor stays at seq-1).
            # BELOW the tip it's a genuine gap (rare — sequences are contiguous); skip it
            # so a missing file can never stall the cursor forever. The primary feeds +
            # EVE Ref backfill anything the fallback skips.
            if seq >= tip:
                break
            log.warning("killstream gap: sequence %s missing below tip %s; skipping", seq, tip)
            state.last_sequence = seq
            seq += 1
            if sleep_s:
                time.sleep(sleep_s)  # pace the skip path too — a run of gaps must stay under 15 req/s
            continue
        scanned += 1
        parsed = r2z2.package_to_ingest(package)
        if parsed is not None:
            killmail_id, killmail_hash, esi = parsed
            if _involves_home(esi, home):
                try:
                    # First-writer-wins: because the fallback is faster it is usually the
                    # first to store these mails, so R2Z2's embedded ESI body becomes the
                    # record. Safe — ESI killmails are immutable and R2Z2 embeds the genuine
                    # ESI body; a later primary fetch is a no-op (idempotent on killmail_id).
                    ingest_killmail(killmail_id, killmail_hash, source=Source.KILLSTREAM, body=esi)
                    ingested += 1
                except Exception as exc:  # noqa: BLE001 — one bad mail must not stop the stream
                    log.warning("killstream ingest failed for %s: %s", killmail_id, exc)
        state.last_sequence = seq
        seq += 1
        if sleep_s:
            time.sleep(sleep_s)

    if ingested:
        try:
            from core.esi.names import backfill_killmail_names

            backfill_killmail_names()
        except Exception:  # noqa: BLE001 — a name-resolution hiccup must not fail the run
            log.warning("killstream name backfill failed", exc_info=True)

    state.last_success_at = timezone.now()
    state.last_run_scanned = scanned
    state.last_run_ingested = ingested
    state.ingested_total = (state.ingested_total or 0) + ingested
    state.last_error = ""
    _save(state)
    IngestSourceHealth.record(SOURCE, count=ingested)
    return {
        "status": "ok", "scanned": scanned, "ingested": ingested,
        "cursor": state.last_sequence, "caught_up": seq >= tip, "gap_skipped": gap_skipped,
    }
