"""PvP rankings for the corp killboard — leaderboards and pilot rank titles.

Built for running fair PvP challenges with prizes: every metric is period-bounded
(including whole calendar months, so last month's winner is exact) and PvP-only —
killmails with no player attacker (``is_npc``: ratting kills, deaths to rats) are
excluded from every metric, the same way zKillboard ignores pure PvE deaths.

A pilot is credited as a home-corp participant when their attacker/victim row
carries the home corporation id, which is historically accurate at the time of
the kill. Kills come off mails where the home corp is the ATTACKER; losses off
mails where it is the VICTIM.

Leaderboards are computed on demand (windows are flexible) and memoized in the
cache for a few minutes — rankings do not need to be real-time.

Serving strategy (P5): the board is public and anonymous, so the cache is the only
thing standing between a link on Reddit and a full-history aggregation per request.
Three mechanisms, all in this module:

* the warmer (``warm_windows``) fills the hot windows for every alt-rollup mode and
  every enabled locale from ONE database pass per window, because only the prose
  differs between locales — see that function for the budget;
* a cached payload keeps a *freshness* marker separate from the value itself, so an
  entry that has gone stale can still be served while a single request rebuilds it;
* that rebuild is single-flighted with the ``cache.add`` owner-token mutex this
  codebase already uses for beat tasks (``killstream``, ``signature_pipeline``), so N
  simultaneous readers of a cold variant cost one build, not N.
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import formats, timezone, translation
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from core.i18n import i18n_cache_key

from .models import Killmail, KillmailParticipant
from .ranks import active_ladder, combat_rank  # noqa: F401  (re-exported for callers)
from .valuation import at_kill_value_expr

# Efficiency is only fair with a body of fights behind it, or one lucky kill
# reads as "100%". Pilots below this many fights (kills + losses) don't rank.
EFFICIENCY_MIN_FIGHTS = 5
TOP_N = 10
TOP_N_KILLS = 5  # the "most valuable kills" highlight list

# Held longer than the warm cadence (every 5 min) so warmed windows never expire
# between cycles; rankings tolerate the resulting few-minutes staleness.
CACHE_TTL = 900
CACHE_VERSION = 1

# The windows whose aggregation is NOT time-bounded (``all``) or spans a quarter /
# a whole closed month. They are the expensive ones — ``all`` scans the entire
# killboard — and they are also the ones nobody lands on by default, so they are
# warmed on the slow (~15 min) cadence rather than every tick and their payload
# stays "fresh" for an hour, mirroring the all-time dashboard slice
# (``analytics.ALLTIME_TTL``). A heavy window therefore costs at most one scan an
# hour on the request path, and normally zero because the slow warm gets there first.
HEAVY_WINDOWS = frozenset({"90d", "lastmonth", "all"})
HEAVY_CACHE_TTL = 3600

# How long past its freshness a payload may still be handed to a reader while one
# other request rebuilds it. Serving a 20-minute-old ranking beats making an
# anonymous visitor wait for a full-history aggregation; the boards are already
# explicitly "not real-time". Only the *rebuild* is skipped, never the answer.
STALE_GRACE = 6 * 3600

# Single-flight mutex for a rebuild. The TTL only has to outlive one ``_build`` —
# it is released in a ``finally`` — and the owner token means a build that somehow
# outruns the TTL can never delete its successor's lock (killstream's idiom).
_LOCK_TTL = 120
# How long a reader with NO payload at all (truly cold AND contended) waits for the
# winner's result before giving up and building it itself. Bounded on purpose: a
# request must never hang on another worker, and correctness outranks the saving.
_LOCK_WAIT_S = 2.0
_LOCK_POLL_S = 0.05


# --- Time windows -----------------------------------------------------------
@dataclass(frozen=True)
class Window:
    key: str
    label: str
    start: object | None  # aware datetime, or None for "all time"
    end: object | None


def _month_start(dt):
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def window_for(key: str) -> Window:
    """Resolve a window key to concrete bounds. Unknown keys fall back to 30d.

    The window KEY is a code value (never translated — it is a query-string value and a
    cache-key component); only the human-readable ``label`` is marked. Month names come
    from ``formats.date_format(..., "F")``, not ``calendar.month_name``: the C library's
    month names are locked to the C locale and would stay English in every language.
    Labels are built per call inside the request/task, so eager ``gettext`` is right.
    """
    now = timezone.now()
    if key == "all":
        return Window("all", _("All time"), None, None)
    if key == "7d":
        return Window("7d", _("Last 7 days"), now - timedelta(days=7), None)
    if key == "90d":
        return Window("90d", _("Last 90 days"), now - timedelta(days=90), None)
    if key == "month":
        start = _month_start(now)
        label = _("This month · %(month)s") % {"month": formats.date_format(now, "F")}
        return Window("month", label, start, None)
    if key == "lastmonth":
        this_start = _month_start(now)
        last_end = this_start
        last_start = _month_start(this_start - timedelta(days=1))
        label = _("Last month · %(month)s") % {"month": formats.date_format(last_start, "F")}
        return Window("lastmonth", label, last_start, last_end)
    return Window("30d", _("Last 30 days"), now - timedelta(days=30), None)


WINDOW_KEYS = ["7d", "30d", "90d", "month", "lastmonth", "all"]


def window_choices() -> list[tuple[str, str]]:
    return [(w.key, w.label) for w in (window_for(k) for k in WINDOW_KEYS)]


def _time_filter(prefix: str, window: Window) -> Q:
    q = Q()
    if window.start is not None:
        q &= Q(**{f"{prefix}__gte": window.start})
    if window.end is not None:
        q &= Q(**{f"{prefix}__lt": window.end})
    return q


# --- Combat rank ladder (gamification) --------------------------------------
# The ladder now lives in the DB (CombatRankTitle) and is served by ``ranks.py``;
# ``combat_rank`` and ``active_ladder`` are imported above and re-exported here for
# the many existing callers. ``RANK_LADDER`` remains as the legacy default the ranks
# service falls back to when no active rank rows exist (kept in sync in ranks.py).
RANK_LADDER = [
    (0, "Capsuleer", "text-faint"),
    (1, "Recruit", "text-muted"),
    (10, "Hunter", "text-cyan"),
    (50, "Killer", "text-gold"),
    (150, "Marauder", "text-gold"),
    (400, "Warlord", "text-kill"),
    (1000, "Apex Predator", "text-kill"),
]


def _newbro_softening() -> tuple:
    """``(soften_enabled, below_events)`` for the danger label, cached (singleton config).

    The cached value is a ``(bool, int)`` — no prose — so this key stays language-neutral
    on purpose: scoping it by language would multiply a hot config read by nine for nothing.
    """
    from django.core.cache import cache

    cached = cache.get("killboard:newbro_soften")
    if cached is None:
        from .models import NewbroConfig

        cfg = NewbroConfig.load()
        cached = (cfg.soften_danger_label, cfg.soften_below_events)
        cache.set("killboard:newbro_soften", cached, 300)
    return cached


def danger_rating(kills: int, losses: int) -> dict:
    """zKill-style threat read from the kill/loss balance.

    Leadership can soften the kill-light "Snuggly" label to "Learning" for pilots below
    an activity floor, so a newbro who simply hasn't fought yet isn't labelled harshly.
    """
    total = kills + losses
    if total == 0:
        return {"label": _("Untested"), "ratio": 0.0, "color": "text-faint"}
    ratio = kills / total
    if ratio >= 0.8:
        return {"label": _("Dangerous"), "ratio": ratio, "color": "text-kill"}
    if ratio >= 0.5:
        return {"label": _("Risky"), "ratio": ratio, "color": "text-gold"}
    soften, below = _newbro_softening()
    if soften and total < below:
        return {"label": _("Learning"), "ratio": ratio, "color": "text-faint"}
    return {"label": _("Snuggly"), "ratio": ratio, "color": "text-loss"}


# --- Per-pilot aggregates ---------------------------------------------------
def _home() -> int:
    return settings.FORCA_HOME_CORP_ID


def _kill_rows(window: Window):
    """Per-pilot kill aggregates for the window (home corp as attacker, PvP only)."""
    return (
        KillmailParticipant.objects.filter(
            _time_filter("killmail__killmail_time", window),
            role=KillmailParticipant.Role.ATTACKER,
            corporation_id=_home(),
            character_id__isnull=False,
            killmail__home_corp_role=Killmail.HomeRole.ATTACKER,
            killmail__is_npc=False,
        )
        .values("character_id")
        .annotate(
            kills=Count("killmail", distinct=True),
            final_blows=Count("killmail", filter=Q(final_blow=True), distinct=True),
            solo_kills=Count("killmail", filter=Q(killmail__is_solo=True), distinct=True),
            # KB-35: rank by the value at the time of the kill (fair across price moves),
            # falling back to the live total for mails not yet backfilled.
            isk_destroyed=Sum(at_kill_value_expr("killmail__")),
            points=Sum("killmail__points"),
        )
    )


def _loss_rows(window: Window):
    """Per-pilot loss aggregates for the window (home corp as victim, PvP only)."""
    return (
        Killmail.objects.filter(
            _time_filter("killmail_time", window),
            # home_corp_role=VICTIM already implies this, but naming the composite index's
            # leading column (involves_home_corp, home_corp_role, killmail_time DESC) makes
            # it seekable instead of scanned — same rows, faster plan.
            involves_home_corp=True,
            home_corp_role=Killmail.HomeRole.VICTIM,
            victim_character_id__isnull=False,
            is_npc=False,
        )
        .values("victim_character_id")
        .annotate(losses=Count("killmail_id"), isk_lost=Sum(at_kill_value_expr()))
    )


def _active_days(window: Window) -> dict[int, int]:
    """Distinct calendar days each pilot got on a killmail (kill or loss).

    A pilot who both killed and died on the same day must count that day ONCE, so the
    two sources have to be de-duplicated against each other. That is done with a SQL
    ``UNION`` (which de-duplicates whole rows) and counted with a ``Counter``, rather
    than by materialising a ``set`` of dates per pilot in Python: on the unbounded
    ``all`` window the old shape allocated one Python object per (pilot, day) pair for
    the whole history of the corp. The result is identical by construction — counting
    the rows of a de-duplicated ``(character_id, day)`` union IS the size of that set —
    and the regression test pins it against a verbatim copy of the old implementation.

    ``order_by()`` clears each model's default ordering, which a combined query cannot
    carry anyway. It is not only a UNION formality: with ``killmail_time`` still in the
    ORDER BY, Postgres has to include it in a ``SELECT DISTINCT``, so the loss side was
    silently returning one row per killmail rather than one per (pilot, day).
    """
    kill_days = (
        KillmailParticipant.objects.filter(
            _time_filter("killmail__killmail_time", window),
            role=KillmailParticipant.Role.ATTACKER,
            corporation_id=_home(),
            character_id__isnull=False,
            killmail__home_corp_role=Killmail.HomeRole.ATTACKER,
            killmail__is_npc=False,
        )
        .annotate(day=TruncDate("killmail__killmail_time"))
        .values_list("character_id", "day")
        .order_by()
        .distinct()
    )
    loss_days = (
        Killmail.objects.filter(
            _time_filter("killmail_time", window),
            involves_home_corp=True,  # implied by home_corp_role=VICTIM; makes the index seekable
            home_corp_role=Killmail.HomeRole.VICTIM,
            victim_character_id__isnull=False,
            is_npc=False,
        )
        .annotate(day=TruncDate("killmail_time"))
        .values_list("victim_character_id", "day")
        .order_by()
        .distinct()
    )
    return dict(Counter(cid for cid, _day in kill_days.union(loss_days)))


def _merge_pilots(window: Window) -> dict[int, dict]:
    """All per-pilot stats for the window, keyed by character_id."""
    pilots: dict[int, dict] = {}
    for r in _kill_rows(window):
        pilots[r["character_id"]] = {
            "character_id": r["character_id"],
            "kills": r["kills"] or 0,
            "final_blows": r["final_blows"] or 0,
            "solo_kills": r["solo_kills"] or 0,
            "isk_destroyed": r["isk_destroyed"] or 0,
            "points": r["points"] or 0,
            "losses": 0,
            "isk_lost": 0,
        }
    for r in _loss_rows(window):
        p = pilots.setdefault(
            r["victim_character_id"],
            {
                "character_id": r["victim_character_id"],
                "kills": 0, "final_blows": 0, "solo_kills": 0,
                "isk_destroyed": 0, "points": 0,
            },
        )
        p["losses"] = r["losses"] or 0
        p["isk_lost"] = r["isk_lost"] or 0
    active = _active_days(window)
    for cid, p in pilots.items():
        p.setdefault("losses", 0)
        p.setdefault("isk_lost", 0)
        p["active_days"] = active.get(cid, 0)
        p["engagements"] = p["kills"] + p["losses"]
        denom = float(p["isk_destroyed"]) + float(p["isk_lost"])
        p["efficiency"] = (float(p["isk_destroyed"]) / denom * 100.0) if denom else 0.0
    return pilots


def _rollup_by_main(pilots: list[dict]) -> list[dict]:
    """Collapse per-character pilot dicts into one dict per person's MAIN (KB-23).

    Sums the additive stat fields across a person's alts and recomputes the derived
    engagements + efficiency, re-keying ``character_id`` to the main so a row renders the
    main's name/portrait (``eve_name`` resolves the id at render). Unlinked pilots map to
    themselves and pass through unchanged.

    Semantics: the per-character stats are already *per-participation* — a kill counts for
    every attacker on the mail — so summing gives "everything this person's pilots did",
    counting a mail once per alt that was on it (the common case, one pilot per mail, is
    exact). This matches how the board already treats participation and how aa-killstats
    aggregates by main; it is not distinct-by-killmail. ``active_days`` likewise sums, so a
    main and an alt both active on the same day nudge the "Most Active" board up slightly.
    Summing keeps the live and historical (per-character-count-only) paths consistent.
    """
    from core.pilots import mains_for

    main_map = mains_for([p["character_id"] for p in pilots])
    merged: dict[int, dict] = {}
    for p in pilots:
        mid = main_map.get(p["character_id"], p["character_id"])
        m = merged.get(mid)
        if m is None:
            merged[mid] = {**p, "character_id": mid}
            continue
        for f in ("kills", "final_blows", "solo_kills", "isk_destroyed",
                  "points", "losses", "isk_lost", "active_days"):
            m[f] = m.get(f, 0) + p.get(f, 0)
    for m in merged.values():
        m["engagements"] = m["kills"] + m["losses"]
        denom = float(m["isk_destroyed"]) + float(m["isk_lost"])
        m["efficiency"] = (float(m["isk_destroyed"]) / denom * 100.0) if denom else 0.0
    return list(merged.values())


def _rank(pilots, value_key, *, predicate=None, secondary=None, limit=TOP_N):
    """Top-N rows sorted by ``value_key`` desc, dropping zero/ineligible rows."""
    rows = [p for p in pilots if (predicate(p) if predicate else p.get(value_key, 0) > 0)]
    rows.sort(key=lambda p: (p.get(value_key, 0), p.get("kills", 0)), reverse=True)
    out = []
    for i, p in enumerate(rows[:limit], start=1):
        out.append({
            "place": i,
            "character_id": p["character_id"],
            "value": p.get(value_key, 0),
            "secondary": secondary(p) if secondary else None,
        })
    return out


def _most_valuable_kills(window: Window) -> list[dict]:
    """Top single kills by value in the window, credited to the home final-blower."""
    mails = (
        Killmail.objects.filter(
            _time_filter("killmail_time", window),
            involves_home_corp=True,  # implied by home_corp_role=ATTACKER; makes the index seekable
            home_corp_role=Killmail.HomeRole.ATTACKER,
            is_npc=False,
        )
        .order_by("-total_value")[:TOP_N_KILLS]
    )
    out = []
    for km in mails:
        fb = (
            km.participants.filter(
                role=KillmailParticipant.Role.ATTACKER,
                corporation_id=_home(),
                final_blow=True,
            ).first()
            or km.participants.filter(
                role=KillmailParticipant.Role.ATTACKER, corporation_id=_home()
            ).order_by("-damage_done").first()
        )
        out.append({
            "killmail_id": km.killmail_id,
            "value": km.total_value,
            "victim_ship_type_id": km.victim_ship_type_id,
            "solar_system_id": km.solar_system_id,
            "killmail_time": km.killmail_time,
            "character_id": fb.character_id if fb else None,
        })
    return out


# key, title, one-line subtitle, value kind ('isk'|'int'|'pct'), icon symbol id.
# The key / kind / icon are CODE values (dict lookups, template symbol ids) and are never
# translated. Titles and subtitles are prose: a module-level constant, so they are marked
# with gettext_lazy — the lazy proxy resolves at the moment a payload is built, under that
# request's locale (see ``_categories``, which forces them to plain ``str`` before caching).
CATEGORIES = [
    ("top_killers", gettext_lazy("Top Killers"), gettext_lazy("Most kills landed"), "int", "i-cross"),
    ("isk_destroyed", gettext_lazy("Most ISK Destroyed"), gettext_lazy("Damage to the enemy wallet"),
     "isk", "i-coin"),
    ("points", gettext_lazy("Top Points"), gettext_lazy("Quality kills, not just blobs"), "int", "i-bolt"),
    ("final_blows", gettext_lazy("Final Blows"), gettext_lazy("Who lands the killing shot"), "int", "i-target"),
    ("solo_kills", gettext_lazy("Solo Kills"), gettext_lazy("1v1 — pure pilot skill"), "int", "i-shield"),
    ("efficiency", gettext_lazy("Best Efficiency"), gettext_lazy("ISK destroyed vs. lost"), "pct", "i-grid"),
    ("most_active", gettext_lazy("Most Active"), gettext_lazy("Days undocked and fighting"), "int", "i-check"),
    ("isk_lost", gettext_lazy("Most ISK Lost"), gettext_lazy("Bravest feeder of the month"), "isk", "i-ship"),
]


def categories_payload(boards: dict) -> list[dict]:
    """The eight category cards for a rankings payload, with their prose resolved.

    ``str(...)`` is deliberate: this list goes straight into a cache entry that is pickled
    into Redis. A lazy proxy pickles fine, but it would re-resolve at *unpickle* time in the
    READER's locale — which is only what we want when the key is language-neutral. These
    payloads ARE language-scoped (``i18n_cache_key``), so the value must be frozen to a plain
    ``str`` at ``cache.set`` time; otherwise a de/fr reader could resolve an entry we already
    keyed as, say, ``:de`` and the two mechanisms would fight each other.
    """
    return [
        {"key": k, "title": str(t), "subtitle": str(s), "kind": kind, "icon": icon, "rows": boards[k]}
        for (k, t, s, kind, icon) in CATEGORIES
    ]


# The per-row "secondary" captions. Built inside the request/task that renders a board, so
# eager gettext (a plain str lands in the cached payload). They were f-strings, which xgettext
# cannot see — wrapping an f-string in gettext is a silent no-op — so the count is interpolated
# with a named placeholder AFTER translation instead. The English wording (incl. "1 kills") is
# kept exactly as it was: ngettext here would change the English output, which is out of scope.
def secondary_captions() -> dict:
    """``{board_key: callable}`` producing each board's per-row caption, translated."""
    return {
        "top_killers": lambda p: _("%(count)s final blows") % {"count": p["final_blows"]},
        "kills": lambda p: _("%(count)s kills") % {"count": p["kills"]},
        "most_active": lambda p: _("%(count)s fights") % {"count": p["engagements"]},
        "isk_lost": lambda p: _("%(count)s losses") % {"count": p["losses"]},
        "efficiency": lambda p: _("%(kills)s–%(losses)s K/L")
        % {"kills": p["kills"], "losses": p["losses"]},
    }


def build_boards(pilots: list[dict], *, limit: int = TOP_N) -> dict:
    """The eight ranked boards for a set of per-pilot rows (shared with the historical path).

    ``limit`` defaults to the display top-10 every existing caller wants. The raffle's
    rank-prize standings pass a deeper limit because they must be able to tell a pilot they
    are 14th and what 13th costs — a board that silently ends at 10 cannot answer that, and
    duplicating this eight-category configuration in another app to get it would guarantee
    the two drift apart.
    """
    cap = secondary_captions()
    return {
        "top_killers": _rank(pilots, "kills", secondary=cap["top_killers"], limit=limit),
        "isk_destroyed": _rank(pilots, "isk_destroyed", secondary=cap["kills"], limit=limit),
        "points": _rank(pilots, "points", secondary=cap["kills"], limit=limit),
        "final_blows": _rank(pilots, "final_blows", limit=limit),
        "solo_kills": _rank(pilots, "solo_kills", limit=limit),
        "most_active": _rank(pilots, "active_days", secondary=cap["most_active"], limit=limit),
        "isk_lost": _rank(pilots, "isk_lost", secondary=cap["isk_lost"], limit=limit),
        "efficiency": _rank(
            pilots, "efficiency",
            predicate=lambda p: (p["kills"] + p["losses"]) >= EFFICIENCY_MIN_FIGHTS,
            secondary=cap["efficiency"], limit=limit,
        ),
    }


# --- Building a payload -----------------------------------------------------
# A rankings payload has two halves, and only one of them is expensive. ``_aggregate``
# is the database half and is language-neutral — ids, counts and ISK are the same in
# every locale. ``_render`` is pure Python over those rows and is where every piece of
# prose (the window label, the category titles, the per-row captions) resolves under the
# active language. Splitting them is what lets the warmer fill 12 board variants across
# every enabled locale from one pass per window instead of one pass per variant per
# locale; ``_build`` glues them back together for the single-payload callers.
def _aggregate(window: Window) -> tuple[list[dict], list[dict]]:
    """``(per-character rows, most-valuable kills)`` for a window — the DB half."""
    return list(_merge_pilots(window).values()), _most_valuable_kills(window)


def _pilot_rows(pilots: list[dict], *, by_main: bool) -> list[dict]:
    """The rows a board ranks: per character, or rolled up under each person's main.

    ``_rollup_by_main`` copies every row it touches, so the caller's list is never
    mutated and one aggregation can safely feed both modes.
    """
    return _rollup_by_main(pilots) if by_main else pilots


def _render(window: Window, pilots: list[dict], most_valuable: list[dict]) -> dict:
    """The payload as a reader sees it, with every string resolved in the active language."""
    return {
        "window": {"key": window.key, "label": str(window.label)},
        "categories": categories_payload(build_boards(pilots)),
        "most_valuable": most_valuable,
        "pilot_count": len(pilots),
        "efficiency_min_fights": EFFICIENCY_MIN_FIGHTS,
    }


def _build(window_key: str, *, by_main: bool = False) -> dict:
    window = window_for(window_key)
    pilots, most_valuable = _aggregate(window)
    return _render(window, _pilot_rows(pilots, by_main=by_main), most_valuable)


# --- Cache plumbing ---------------------------------------------------------
def _cache_keys(window_key: str, by_main: bool) -> tuple[str, str, str]:
    """``(payload, freshness marker, rebuild lock)`` keys for one board variant.

    The payload key is unchanged and still holds the plain payload dict: freshness lives
    in a SEPARATE marker key rather than in an envelope around the value, so every other
    reader of ``kb:lb:…`` (and the invalidation sweeps) keeps seeing exactly what it saw
    before. The marker expires first; the payload outlives it by ``STALE_GRACE`` and is
    what a reader gets served while one request rebuilds.
    """
    suffix = ":main" if by_main else ""
    key = i18n_cache_key(f"kb:lb:{CACHE_VERSION}:{_home()}:{window_key}{suffix}")
    return key, f"{key}:fresh", f"{key}:lock"


def _fresh_ttl(window_key: str) -> int:
    return HEAVY_CACHE_TTL if window_key in HEAVY_WINDOWS else CACHE_TTL


def _store(key: str, fresh_key: str, payload: dict, fresh_ttl: int) -> None:
    cache.set(key, payload, fresh_ttl + STALE_GRACE)
    cache.set(fresh_key, True, fresh_ttl)


def _revalidate(window_key: str, *, by_main: bool, stale: dict | None) -> dict:
    """Rebuild one variant under a single-flight lock, serving ``stale`` to the losers.

    Exactly one caller per variant runs ``_build``; everyone else either gets the
    slightly-stale payload immediately (the normal case once a board has ever been
    built) or, with nothing cached at all, waits a bounded moment for the winner and
    only then builds it themselves. The fallback build is deliberate: a bug in the
    lock must degrade to today's behaviour — slow — never to an empty board.
    """
    key, fresh_key, lock_key = _cache_keys(window_key, by_main)
    fresh_ttl = _fresh_ttl(window_key)
    token = uuid4().hex
    if cache.add(lock_key, token, _LOCK_TTL):
        try:
            payload = _build(window_key, by_main=by_main)
            _store(key, fresh_key, payload, fresh_ttl)
            return payload
        finally:
            if cache.get(lock_key) == token:  # only free our own lock (overrun-safe)
                cache.delete(lock_key)
    if stale is not None:
        return stale
    deadline = time.monotonic() + _LOCK_WAIT_S
    while time.monotonic() < deadline:
        time.sleep(_LOCK_POLL_S)
        payload = cache.get(key)
        if payload is not None:
            return payload
    payload = _build(window_key, by_main=by_main)
    _store(key, fresh_key, payload, fresh_ttl)
    return payload


def leaderboards(
    window_key: str, *, use_cache: bool = True, refresh: bool = False, by_main: bool = False
) -> dict:
    """Full rankings payload for a window (memoized in the cache).

    ``refresh=True`` rebuilds and re-caches even on a hit — used by the warmer.
    ``by_main=True`` rolls a person's alts up under their main (KB-23), cached separately.

    A miss does NOT simply build. This is an anonymous public page with a window selector,
    so a cold variant can be requested by many visitors at once (and ``?window=all`` scans
    the whole killboard): the rebuild is single-flighted, and a payload that is merely past
    its freshness is served as-is to everyone but the one request doing the rebuild.

    Language-scoped key: the payload carries prose (the window label with its month name,
    the eight category titles/subtitles, each row's secondary caption, the danger labels).
    """
    if window_key not in WINDOW_KEYS:
        window_key = "30d"
    if not use_cache:
        return _build(window_key, by_main=by_main)
    key, fresh_key, _lock_key = _cache_keys(window_key, by_main)
    if refresh:
        payload = _build(window_key, by_main=by_main)
        _store(key, fresh_key, payload, _fresh_ttl(window_key))
        return payload
    found = cache.get_many([key, fresh_key])
    payload = found.get(key)
    if payload is not None and found.get(fresh_key):
        return payload
    return _revalidate(window_key, by_main=by_main, stale=payload)


def warm_windows(window_keys, languages) -> int:
    """Pre-build every ``(window × alt-rollup mode × language)`` board variant. Returns
    the number of cache entries written.

    The point is the loop nesting. A variant differs from its siblings only in prose (the
    locale) or in a pure-Python regrouping of the same rows (``by_main``), so the database
    is touched ONCE per window and the other 2 × len(languages) − 1 variants are rendered
    off those rows. The previous warmer re-ran the whole aggregation per window per locale
    and still left 9 of the 12 reachable variants permanently cold, so the by-main toggle
    and every non-default window paid a full cold build on the request path — in the worst
    case a full-history scan, on a public page, on spinning disks.

    Budget, on the 5-minute beat and 8 enabled locales: 3 hot windows = 3 aggregations per
    tick (was 24) and 48 cheap renders, plus the 3 heavy windows on the ~15-minute cadence.
    Widening the coverage 4× therefore *lowers* the warmer's database cost; the renders and
    ``cache.set``s are what grew, and those are Python and Redis, not the HDD RAID.
    """
    warmed = 0
    for window_key in window_keys:
        pilots, most_valuable = _aggregate(window_for(window_key))
        fresh_ttl = _fresh_ttl(window_key)
        for by_main in (False, True):
            rows = _pilot_rows(pilots, by_main=by_main)
            for code in languages:
                with translation.override(code):
                    # Re-resolved inside the override for its translated label (the window
                    # bounds are not part of the payload); the rows are already computed.
                    payload = _render(window_for(window_key), rows, most_valuable)
                    key, fresh_key, _lock = _cache_keys(window_key, by_main)
                    _store(key, fresh_key, payload, fresh_ttl)
                warmed += 1
    return warmed


def _card_from(character_id, *, kills, losses, solo_kills, final_blows, points,
               isk_destroyed, isk_lost) -> dict:
    denom = float(isk_destroyed) + float(isk_lost)
    return {
        "has_record": kills > 0 or losses > 0,
        "character_id": character_id,
        "kills": kills,
        "losses": losses,
        "solo_kills": solo_kills,
        "final_blows": final_blows,
        "points": points,
        "isk_destroyed": isk_destroyed,
        "isk_lost": isk_lost,
        "efficiency": (float(isk_destroyed) / denom * 100.0) if denom else 0.0,
        "solo_pct": (solo_kills / kills * 100.0) if kills else 0.0,
        "rank": combat_rank(kills),
        "danger": danger_rating(kills, losses),
    }


def _card_live(character_id: int) -> dict:
    window = window_for("all")
    kr = _kill_rows(window).filter(character_id=character_id).order_by("character_id").first() or {}
    lr = (
        _loss_rows(window)
        .filter(victim_character_id=character_id)
        .order_by("victim_character_id")
        .first()
        or {}
    )
    return _card_from(
        character_id,
        kills=kr.get("kills", 0) or 0,
        losses=lr.get("losses", 0) or 0,
        solo_kills=kr.get("solo_kills", 0) or 0,
        final_blows=kr.get("final_blows", 0) or 0,
        points=kr.get("points", 0) or 0,
        isk_destroyed=kr.get("isk_destroyed", 0) or 0,
        isk_lost=lr.get("isk_lost", 0) or 0,
    )


def pilot_combat_card(character_id: int, *, use_cache: bool = True) -> dict:
    """All-time PvP standing for one pilot: rank title, danger, solo %, totals.

    Served from the per-pilot ``CombatMetric`` rollup (rebuilt nightly) when a row
    exists — a single indexed read — and falls back to a live per-pilot
    aggregation otherwise (a pilot who isn't in the rollup yet, or before the
    first rebuild). Memoized briefly either way; it sits on the dashboard hot path.

    Language-scoped key: the card embeds the translated ``danger`` label.
    """
    from .models import CombatMetric

    key = i18n_cache_key(f"kb:card:{CACHE_VERSION}:{_home()}:{character_id}")
    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            return cached

    row = (
        CombatMetric.objects.filter(
            entity_type=CombatMetric.EntityType.CHARACTER, entity_id=character_id, window="all"
        )
        .values("kills", "losses", "solo_kills", "final_blows", "points",
                "isk_destroyed", "isk_lost")
        .first()
    )
    if row is not None:
        card = _card_from(
            character_id,
            kills=row["kills"], losses=row["losses"], solo_kills=row["solo_kills"],
            final_blows=row["final_blows"], points=row["points"],
            isk_destroyed=row["isk_destroyed"], isk_lost=row["isk_lost"],
        )
    else:
        card = _card_live(character_id)
    if use_cache:
        cache.set(key, card, CACHE_TTL)
    return card


def corp_combat_roster(*, use_cache: bool = True, refresh: bool = False) -> list[dict]:
    """Every corp pilot with their all-time PvP standing, ordered by name.

    A colleague-facing directory: one bulk aggregation (not a card per pilot),
    joined to the corp member list so even pilots with no killmail yet appear
    (Untested). Each row carries a rank title, danger rating and totals plus the
    character id the caller turns into a link to that pilot's analytics page.

    ``refresh=True`` rebuilds and re-caches even on a hit — used by the warmer so
    the first member after each TTL lapse never pays the all-time recompute.

    Language-scoped key: every row embeds the translated ``danger`` label.
    """
    from apps.sso.models import EveCharacter

    key = i18n_cache_key(f"kb:roster:{CACHE_VERSION}:{_home()}")
    if use_cache and not refresh:
        cached = cache.get(key)
        if cached is not None:
            return cached

    stats = _merge_pilots(window_for("all"))
    members = (
        EveCharacter.objects.filter(is_corp_member=True)
        .exclude(name="")
        .order_by("name")
        .values("character_id", "name", "corporation_id")
    )
    ladder = active_ladder()  # fetch once — avoids a cache read per member row
    roster = []
    for m in members:
        cid = m["character_id"]
        s = stats.get(cid, {})
        kills = s.get("kills", 0)
        losses = s.get("losses", 0)
        roster.append({
            "character_id": cid,
            "name": m["name"],
            "corporation_id": m["corporation_id"],
            "kills": kills,
            "losses": losses,
            "efficiency": s.get("efficiency", 0.0),
            "active_days": s.get("active_days", 0),
            "rank": combat_rank(kills, ladder),
            "danger": danger_rating(kills, losses),
            "has_record": kills > 0 or losses > 0,
        })
    if use_cache:
        cache.set(key, roster, CACHE_TTL)
    return roster
