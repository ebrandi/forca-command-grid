"""KB-29 — the outbound realtime killmail feed (SSE + poll), and its event emission.

This module owns three things:

* **Emission** (:func:`emit_stream_event`) — called from the single post-ingest seam
  (:func:`apps.killboard.ingest.ingest_killmail`), so *every* ingest path (ESI corp/char
  feed, the zKill query poll, the R2Z2 killstream, and the EVE Ref / zKill history backfills)
  is covered by one call. It appends a compact :class:`KillboardStreamEvent` ring-buffer row,
  denormalising the topic-filter dimensions so serving the feed never re-queries ``Killmail``.
  It emits **only for kills within ~48h of now**, so a years-deep EVE Ref backfill never floods
  the live feed.

* **Serving** (:func:`stream_response`, :func:`poll_batch`) — the shapes the API view returns.
  The stack is small (see the *worker budget* note below), so the primary shape is a **bounded
  SSE** stream and the fallback is a cheap **short-poll** over the identical cursor contract.

* **Housekeeping** (:func:`prune_events`) — ring-buffer trim run from a beat task.

Worker-budget assessment (why bounded SSE + poll fallback, not unbounded SSE)
----------------------------------------------------------------------------
Production serves the whole suite on gunicorn ``gthread`` with ``GUNICORN_WORKERS`` (default 3)
× ``GUNICORN_THREADS`` (default 4) = **12 concurrent request slots total** (see
``docker-compose.prod.yml``). A long-lived SSE response occupies exactly one of those threads
for its whole lifetime, so unbounded streaming would starve the site. The design keeps SSE
safe on that budget:

* a **hard connection cap** (``KILLBOARD_STREAM_MAX_CLIENTS``, default 4 — a third of the pool
  at worst) enforced by a Redis-cache semaphore; when full the endpoint returns **503 +
  Retry-After** and the client degrades to polling, so extra viewers are never broken;
* a **bounded lifetime** (``KILLBOARD_STREAM_MAX_LIFETIME_S``, default 120s): every stream
  auto-closes and the client resumes from ``Last-Event-ID`` — this both bounds how long any
  thread is held and lets a wedged slot cycle;
* a **heartbeat** (``KILLBOARD_STREAM_HEARTBEAT_S``, default 15s) that keeps the connection and
  nginx's read timeout alive and prunes dead clients;
* a **short-poll** mode (``?mode=poll``) that holds a thread only for one indexed query — the
  automatic fallback when the SSE cap is full or the feature is disabled, and the cheaper shape
  for bots. Both modes share the ``seq`` cursor, so a consumer can move between them freely.

Operators expecting many concurrent live viewers should raise ``GUNICORN_THREADS`` /
``GUNICORN_WORKERS`` (and then ``KILLBOARD_STREAM_MAX_CLIENTS``) together — the cap must stay a
minority of the thread pool. See ``handbooks/operator-handbook/operations-runbook.md``.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.http import StreamingHttpResponse
from django.utils import timezone

from .models import KillboardStreamEvent, Killmail

log = logging.getLogger("forca.killboard")


# --------------------------------------------------------------------------- #
#  Settings accessors (all env-overridable; see config/settings/base.py)
# --------------------------------------------------------------------------- #
def _enabled() -> bool:
    return bool(getattr(settings, "KILLBOARD_STREAM_ENABLED", True))


def _max_clients() -> int:
    return int(getattr(settings, "KILLBOARD_STREAM_MAX_CLIENTS", 4))


def _heartbeat_s() -> float:
    return float(getattr(settings, "KILLBOARD_STREAM_HEARTBEAT_S", 15))


def _max_lifetime_s() -> float:
    return float(getattr(settings, "KILLBOARD_STREAM_MAX_LIFETIME_S", 120))


def _poll_interval_s() -> float:
    return float(getattr(settings, "KILLBOARD_STREAM_POLL_INTERVAL_S", 2))


def _fresh_window() -> timedelta:
    return timedelta(hours=int(getattr(settings, "KILLBOARD_STREAM_FRESH_HOURS", 48)))


def _batch() -> int:
    return int(getattr(settings, "KILLBOARD_STREAM_BATCH", 200))


def _retention() -> int:
    return int(getattr(settings, "KILLBOARD_STREAM_RETENTION", 10000))


# --------------------------------------------------------------------------- #
#  Emission — called from the single post-ingest seam (covers all ingest paths)
# --------------------------------------------------------------------------- #
def emit_stream_event(km: Killmail) -> KillboardStreamEvent | None:
    """Append a stream event for a freshly-ingested home-corp killmail, or return ``None``.

    Skips silently (no row) when the feature is off, the mail doesn't involve the home corp,
    or the kill is older than the freshness window (a backfilled historical mail). Never
    raises into the caller — a stream hiccup must not break ingestion.
    """
    if not _enabled() or not km.involves_home_corp:
        return None
    if km.killmail_time < timezone.now() - _fresh_window():
        return None  # a backfilled historical mail — never floods the live feed
    try:
        return _write_event(km)
    except Exception:  # noqa: BLE001 — the outbound feed must never break an ingest
        log.warning("stream emit failed for killmail %s", km.killmail_id, exc_info=True)
        return None


def _write_event(km: Killmail) -> KillboardStreamEvent:
    is_loss = km.home_corp_role == Killmail.HomeRole.VICTIM
    return KillboardStreamEvent.objects.create(
        killmail=km,
        killmail_hash=km.killmail_hash,
        kill_time=km.killmail_time,
        home_role=km.home_corp_role,
        sec_band=km.sec_band,
        system_id=km.solar_system_id,
        ship_class=_ship_class(km.victim_ship_type_id),
        victim_ship_type_id=km.victim_ship_type_id,
        victim_character_id=km.victim_character_id,
        victim_corporation_id=km.victim_corporation_id,
        total_value=km.total_value,
        is_solo=km.is_solo,
        is_npc=km.is_npc,
        is_awox=km.is_awox,
        needs_srp=_needs_srp(km) if is_loss else False,
        deviated=_is_deviated(km) if is_loss else False,
    )


def _ship_class(ship_type_id: int | None) -> str:
    if not ship_type_id:
        return "Other"
    try:
        from apps.doctrines.hulls import hull_meta

        meta = hull_meta({ship_type_id}).get(ship_type_id)
        return meta["hull_class"] if meta else "Other"
    except Exception:  # noqa: BLE001 — classification is best-effort, never fatal
        return "Other"


def _is_deviated(km: Killmail) -> bool:
    """True when the loss diverged from its matched doctrine fit (the deviation was just
    computed by ``compute_fit_deviation`` at ingest, so this is a cheap attribute read)."""
    deviation = getattr(km, "fit_deviation", None)
    return bool(deviation is not None and not deviation.is_clean)


def _needs_srp(km: Killmail) -> bool:
    """Precompute the ``needs-srp`` topic flag once, at emission, so the stream stays cheap.

    A brand-new loss can't have a claim yet, so this is purely the eligibility verdict. The
    SRP service does several reads; it is called only for losses and only once per killmail,
    and any failure degrades to ``False`` rather than breaking ingestion.
    """
    try:
        from apps.srp.services import eligibility

        return bool(eligibility(km).get("eligible"))
    except Exception:  # noqa: BLE001 — SRP is optional; never fail ingest over it
        log.debug("needs_srp probe failed for killmail %s", km.killmail_id, exc_info=True)
        return False


# --------------------------------------------------------------------------- #
#  Topics — parse the ?topics= list into a matcher, enforcing RBAC on gated ones
# --------------------------------------------------------------------------- #
# Topics only members may subscribe to (they expose the doctrine/SRP posture of a loss).
MEMBER_TOPICS = {"deviated-losses", "needs-srp"}


class TopicError(ValueError):
    """A requested topic is malformed, unknown, or gated above the caller's tier."""

    def __init__(self, message: str, *, forbidden: bool = False):
        super().__init__(message)
        self.forbidden = forbidden


def build_matcher(raw_topics: str | None, *, member: bool):
    """Compile ``?topics=`` (a comma list) into ``predicate(event) -> bool``.

    Topics are OR-combined: an event matches if it satisfies *any* requested topic. An empty
    or missing list means ``all``. Raises :class:`TopicError` for an unknown/malformed topic,
    or (``forbidden=True``) when a non-member requests a member-gated topic.
    """
    names = [t.strip() for t in (raw_topics or "").split(",") if t.strip()] or ["all"]
    predicates = []
    for name in names:
        if name in MEMBER_TOPICS and not member:
            raise TopicError(f"Topic '{name}' requires membership.", forbidden=True)
        predicates.append(_topic_predicate(name))
    return lambda ev: any(pred(ev) for pred in predicates)


def _topic_predicate(name: str):
    if name == "all":
        return lambda ev: True
    if name == "kills":
        return lambda ev: ev.home_role == Killmail.HomeRole.ATTACKER
    if name == "losses":
        return lambda ev: ev.home_role == Killmail.HomeRole.VICTIM
    if name == "deviated-losses":
        return lambda ev: ev.deviated
    if name == "needs-srp":
        return lambda ev: ev.needs_srp

    key, _, arg = name.partition(":")
    if not arg:
        raise TopicError(f"Unknown topic '{name}'.")
    if key == "secband":
        return lambda ev, band=arg: ev.sec_band == band
    if key == "shipclass":
        return lambda ev, cls=arg: ev.ship_class == cls
    if key == "system":
        sid = _int(arg, name)
        return lambda ev, sid=sid: ev.system_id == sid
    if key == "pilot":
        cid = _int(arg, name)
        return lambda ev, cid=cid: ev.victim_character_id == cid
    if key == "iskband":
        floor = _int(arg, name)
        return lambda ev, floor=floor: ev.total_value >= floor
    raise TopicError(f"Unknown topic '{name}'.")


def _int(value: str, topic: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise TopicError(f"Topic '{topic}' needs an integer argument.") from None


# --------------------------------------------------------------------------- #
#  Payload — compact, tier-aware JSON (no sensitive fields ever)
# --------------------------------------------------------------------------- #
def event_payload(ev: KillboardStreamEvent, *, member: bool) -> dict:
    """The wire shape of one event. ``needs_srp`` / ``deviated`` reach members only."""
    flags = {"solo": ev.is_solo, "npc": ev.is_npc, "awox": ev.is_awox}
    if member:
        flags["needs_srp"] = ev.needs_srp
        flags["deviated"] = ev.deviated
    return {
        "seq": ev.seq,
        "killmail_id": ev.killmail_id,
        "hash": ev.killmail_hash,
        "kill_time": ev.kill_time.isoformat(),
        "home_role": ev.home_role,
        "sec_band": ev.sec_band,
        "system_id": ev.system_id,
        "ship_class": ev.ship_class,
        "value": str(ev.total_value),
        "victim": {
            "character_id": ev.victim_character_id,
            "corporation_id": ev.victim_corporation_id,
            "ship_type_id": ev.victim_ship_type_id,
        },
        "flags": flags,
    }


# --------------------------------------------------------------------------- #
#  Cursor / query helpers (shared by SSE + poll)
# --------------------------------------------------------------------------- #
def tip_seq() -> int:
    """The newest event seq (0 when the buffer is empty) — the default 'start from now' cursor."""
    from django.db.models import Max

    return KillboardStreamEvent.objects.aggregate(m=Max("seq"))["m"] or 0


def _events_after(cursor: int):
    """A bounded ascending batch of events newer than ``cursor`` (never joins Killmail)."""
    return list(
        KillboardStreamEvent.objects.filter(seq__gt=cursor).order_by("seq")[: _batch()]
    )


def collect(cursor: int, matcher) -> tuple[list[KillboardStreamEvent], int]:
    """Events after ``cursor`` that pass ``matcher``, plus the new cursor (advanced past the
    whole scanned batch, so filtered-out events don't stall resume)."""
    batch = _events_after(cursor)
    matched = [ev for ev in batch if matcher(ev)]
    new_cursor = batch[-1].seq if batch else cursor
    return matched, new_cursor


# --------------------------------------------------------------------------- #
#  Connection semaphore (Redis-cache slots; frees on TTL if a worker is killed)
# --------------------------------------------------------------------------- #
def _slot_ttl() -> int:
    # Comfortably above the max lifetime so a normal stream's slot is released by its own
    # `finally`, and a crashed worker's slot self-heals shortly after it would have closed.
    return int(_max_lifetime_s()) + 30


def acquire_slot() -> str | None:
    """Claim one of ``KILLBOARD_STREAM_MAX_CLIENTS`` slots, or ``None`` when the pool is full.

    Each slot is a distinct cache key claimed with an atomic ``add`` (SETNX); the returned
    owner token must be handed back to :func:`release_slot` so only the claimer can free it.
    """
    cap = _max_clients()
    if cap <= 0:
        return None
    token = uuid.uuid4().hex
    ttl = _slot_ttl()
    for i in range(cap):
        if cache.add(f"kb:stream:slot:{i}", token, timeout=ttl):
            return f"{i}:{token}"
    return None


def release_slot(handle: str | None) -> None:
    if not handle:
        return
    idx, _, token = handle.partition(":")
    key = f"kb:stream:slot:{idx}"
    if cache.get(key) == token:  # only free our own slot
        cache.delete(key)


# --------------------------------------------------------------------------- #
#  SSE — bounded, heartbeated, resumable
# --------------------------------------------------------------------------- #
def _sse(line_id: int | None = None, event: str | None = None, data: str | None = None,
         comment: str | None = None) -> str:
    out = []
    if comment is not None:
        out.append(f": {comment}")
    if line_id is not None:
        out.append(f"id: {line_id}")
    if event is not None:
        out.append(f"event: {event}")
    if data is not None:
        out.append(f"data: {data}")
    return "\n".join(out) + "\n\n"


def _sse_stream(cursor: int, matcher, *, member: bool, slot: str | None):
    """The generator body of the SSE response. Drains the backlog once, then heartbeats until
    the lifetime deadline. Releases the semaphore slot on exit (normal or client disconnect)."""
    heartbeat = _heartbeat_s()
    poll_interval = _poll_interval_s()
    deadline = time.monotonic() + _max_lifetime_s()
    try:
        # Advise the client's reconnect interval, then flush anything already newer than the
        # cursor (gap-fill on a resume; nothing on a fresh 'from now' connection).
        yield f"retry: {int(poll_interval * 1000)}\n\n"
        matched, cursor = collect(cursor, matcher)
        for ev in matched:
            yield _sse(line_id=ev.seq, event=_event_name(ev),
                       data=json.dumps(event_payload(ev, member=member)))

        last_beat = time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            matched, cursor = collect(cursor, matcher)
            if matched:
                for ev in matched:
                    yield _sse(line_id=ev.seq, event=_event_name(ev),
                               data=json.dumps(event_payload(ev, member=member)))
                last_beat = time.monotonic()
            elif time.monotonic() - last_beat >= heartbeat:
                yield _sse(comment="ping")
                last_beat = time.monotonic()
    finally:
        release_slot(slot)


def _event_name(ev: KillboardStreamEvent) -> str:
    return "loss" if ev.home_role == Killmail.HomeRole.VICTIM else "kill"


def stream_response(cursor: int, matcher, *, member: bool, slot: str | None) -> StreamingHttpResponse:
    """Build the bounded SSE :class:`StreamingHttpResponse`. Caller has already acquired ``slot``."""
    resp = StreamingHttpResponse(
        _sse_stream(cursor, matcher, member=member, slot=slot),
        content_type="text/event-stream",
    )
    resp["Cache-Control"] = "no-cache, no-transform"
    # Belt-and-suspenders with nginx `proxy_buffering off` — some proxies honour this header
    # instead of / as well as the location directive.
    resp["X-Accel-Buffering"] = "no"
    resp["Connection"] = "keep-alive"
    return resp


# --------------------------------------------------------------------------- #
#  Poll — one-shot JSON batch over the same cursor contract (the fallback shape)
# --------------------------------------------------------------------------- #
def poll_batch(cursor: int, matcher, *, member: bool) -> dict:
    """Return every matching event after ``cursor`` in one cheap indexed query.

    ``has_more`` is true when the raw (pre-filter) batch was capped, so a client that is far
    behind keeps polling until it drains. ``cursor`` advances past the whole scanned batch.
    """
    batch = _events_after(cursor)
    matched = [ev for ev in batch if matcher(ev)]
    new_cursor = batch[-1].seq if batch else cursor
    return {
        "events": [event_payload(ev, member=member) for ev in matched],
        "cursor": new_cursor,
        "has_more": len(batch) >= _batch(),
    }


# --------------------------------------------------------------------------- #
#  Housekeeping — ring-buffer prune (run from a beat task)
# --------------------------------------------------------------------------- #
def prune_events() -> dict:
    """Trim the ring buffer to the newest ``KILLBOARD_STREAM_RETENTION`` rows. Idempotent."""
    keep = _retention()
    threshold = (
        KillboardStreamEvent.objects.order_by("-seq")
        .values_list("seq", flat=True)[keep : keep + 1]
        .first()
    )
    if threshold is None:
        return {"status": "ok", "deleted": 0}
    deleted, _ = KillboardStreamEvent.objects.filter(seq__lte=threshold).delete()
    return {"status": "ok", "deleted": deleted}
