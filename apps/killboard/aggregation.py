"""Per-pilot, per-calendar-month PvP aggregate — the fast path for historical
rankings over a 147k+ killmail board.

``MonthlyPilotKillStat`` is the only new rollup: one row per (pilot, year, month)
carrying the same PvP-only figures the live leaderboards use, so a past month — or a
whole year (summed across its months) — reproduces the exact same eight boards
without scanning the raw killmails on every request. It is built by a batched,
idempotent, resumable backfill and refreshed incrementally for the current month.

All definitions mirror ``leaderboards.py`` precisely: kills come off home-corp
attacker participant rows on non-NPC mails (distinct killmails — a fleet kill counts
once per pilot), losses off home-corp victim mails. Both aggregations clear the
models' default ``Meta.ordering`` with ``.order_by()`` before ``.values().annotate()``
so the ordering column can't leak into the GROUP BY and inflate the counts.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import formats, timezone, translation

from core.i18n import i18n_cache_key

from .models import Killmail, KillmailParticipant, MonthlyPilotKillStat

_EVE_TZ = UTC  # EVE runs on UTC; pin month boundaries to it (matches analytics.py)
CACHE_VERSION = 1
CACHE_TTL = 900

_ATTACKER = Killmail.HomeRole.ATTACKER
_VICTIM = Killmail.HomeRole.VICTIM


def _home() -> int:
    return getattr(settings, "FORCA_HOME_CORP_ID", 0)


def _period_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    """[start, end) aware datetimes for a calendar month (UTC)."""
    start = datetime(year, month, 1, tzinfo=_EVE_TZ)
    end = datetime(year + 1, 1, 1, tzinfo=_EVE_TZ) if month == 12 else datetime(
        year, month + 1, 1, tzinfo=_EVE_TZ
    )
    return start, end


# --------------------------------------------------------------------------- #
#  Building the aggregate
# --------------------------------------------------------------------------- #
def rebuild_month(year: int, month: int) -> int:
    """Recompute every pilot's row for one calendar month. Idempotent.

    Upserts the fresh figures and prunes any stale rows for the period (pilots who
    dropped out after a revalue/re-ingest), so re-running always converges to the
    exact current truth for that month.
    """
    home = _home()
    if not home:
        return 0
    start, end = _period_bounds(year, month)

    kill_rows = (
        KillmailParticipant.objects.filter(
            role=KillmailParticipant.Role.ATTACKER,
            corporation_id=home,
            character_id__isnull=False,
            killmail__home_corp_role=_ATTACKER,
            killmail__is_npc=False,
            killmail__killmail_time__gte=start,
            killmail__killmail_time__lt=end,
        )
        .order_by()
        .values("character_id")
        .annotate(
            kills=Count("killmail", distinct=True),
            final_blows=Count("killmail", filter=Q(final_blow=True), distinct=True),
            solo_kills=Count("killmail", filter=Q(killmail__is_solo=True), distinct=True),
            isk_destroyed=Sum("killmail__total_value"),
            points=Sum("killmail__points"),
        )
    )
    loss_rows = (
        Killmail.objects.filter(
            involves_home_corp=True,
            home_corp_role=_VICTIM,
            victim_character_id__isnull=False,
            is_npc=False,
            killmail_time__gte=start,
            killmail_time__lt=end,
        )
        .order_by()  # REQUIRED: Killmail.Meta.ordering would pollute the GROUP BY
        .values("victim_character_id")
        .annotate(losses=Count("killmail_id"), isk_lost=Sum("total_value"))
    )

    data: dict[int, dict] = {}
    for r in kill_rows:
        data[r["character_id"]] = {
            "kills": r["kills"] or 0,
            "final_blows": r["final_blows"] or 0,
            "solo_kills": r["solo_kills"] or 0,
            "isk_destroyed": r["isk_destroyed"] or 0,
            "points": r["points"] or 0,
            "losses": 0,
            "isk_lost": 0,
        }
    for r in loss_rows:
        d = data.setdefault(
            r["victim_character_id"],
            {"kills": 0, "final_blows": 0, "solo_kills": 0, "isk_destroyed": 0,
             "points": 0, "losses": 0, "isk_lost": 0},
        )
        d["losses"] = r["losses"] or 0
        d["isk_lost"] = r["isk_lost"] or 0

    for cid, days in _active_days(start, end, home).items():
        if cid in data:
            data[cid]["active_days"] = days

    count = 0
    for cid, d in data.items():
        MonthlyPilotKillStat.objects.update_or_create(
            character_id=cid, year=year, month=month,
            defaults={
                "kills": d["kills"], "losses": d["losses"], "solo_kills": d["solo_kills"],
                "final_blows": d["final_blows"], "isk_destroyed": d["isk_destroyed"],
                "isk_lost": d["isk_lost"], "points": d["points"],
                "active_days": d.get("active_days", 0),
            },
        )
        count += 1
    # Prune pilots who no longer have a record in this period.
    MonthlyPilotKillStat.objects.filter(year=year, month=month).exclude(
        character_id__in=list(data.keys())
    ).delete()
    invalidate_period_cache(year, month)
    return count


def _active_days(start: datetime, end: datetime, home: int) -> dict[int, int]:
    """Distinct calendar days each pilot got on a killmail (kill or loss) in [start,end)."""
    days: dict[int, set] = defaultdict(set)
    kill_days = (
        KillmailParticipant.objects.filter(
            role=KillmailParticipant.Role.ATTACKER,
            corporation_id=home,
            character_id__isnull=False,
            killmail__home_corp_role=_ATTACKER,
            killmail__is_npc=False,
            killmail__killmail_time__gte=start,
            killmail__killmail_time__lt=end,
        )
        .annotate(day=TruncDate("killmail__killmail_time", tzinfo=_EVE_TZ))
        .values_list("character_id", "day")
        .distinct()
    )
    loss_days = (
        Killmail.objects.filter(
            involves_home_corp=True,
            home_corp_role=_VICTIM,
            victim_character_id__isnull=False,
            is_npc=False,
            killmail_time__gte=start,
            killmail_time__lt=end,
        )
        .annotate(day=TruncDate("killmail_time", tzinfo=_EVE_TZ))
        .values_list("victim_character_id", "day")
        .distinct()
    )
    for cid, day in list(kill_days) + list(loss_days):
        days[cid].add(day)
    return {cid: len(ds) for cid, ds in days.items()}


def _earliest_period() -> tuple[int, int] | None:
    """(year, month) of the earliest home-corp PvP killmail, or None if the board is empty."""
    first = (
        Killmail.objects.filter(involves_home_corp=True, is_npc=False)
        .order_by("killmail_time")
        .values_list("killmail_time", flat=True)
        .first()
    )
    if first is None:
        return None
    t = first.astimezone(_EVE_TZ)
    return t.year, t.month


def _iter_periods(start: tuple[int, int], end: tuple[int, int]):
    y, m = start
    while (y, m) <= end:
        yield y, m
        m += 1
        if m == 13:
            m, y = 1, y + 1


def backfill(*, since: tuple[int, int] | None = None, log=None) -> int:
    """Rebuild the aggregate for every month from the earliest killmail to now.

    Idempotent and resumable (pass ``since=(year, month)`` to resume). Works one
    calendar month at a time — each ``rebuild_month`` touches only that month's
    rows, so it never locks the whole table for long. Returns total pilot-rows written.
    """
    home = _home()
    if not home:
        return 0
    earliest = _earliest_period()
    if earliest is None:
        return 0
    start = since or earliest
    now = timezone.now().astimezone(_EVE_TZ)
    end = (now.year, now.month)
    total = 0
    for y, m in _iter_periods(start, end):
        n = rebuild_month(y, m)
        total += n
        if log:
            log(f"  {y}-{m:02d}: {n} pilot-rows")
    return total


def refresh_current_months(n_months: int = 2) -> int:
    """Rebuild the current + previous calendar month(s) — the incremental update path.

    New killmails only ever land in the current (occasionally previous) month, so
    refreshing a small trailing window keeps historical rankings live cheaply.
    """
    now = timezone.now().astimezone(_EVE_TZ)
    y, m = now.year, now.month
    total = 0
    for _ in range(max(1, n_months)):
        total += rebuild_month(y, m)
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return total


# --------------------------------------------------------------------------- #
#  Cache invalidation
# --------------------------------------------------------------------------- #
def _hist_base_key(year: int, month: int | None, *, by_main: bool = False) -> str:
    return f"kb:hist:{CACHE_VERSION}:{_home()}:{year}:{month or 0}{':main' if by_main else ''}"


def _hist_key(year: int, month: int | None, *, by_main: bool = False) -> str:
    """Language-scoped: a historical board embeds prose (the period label with its month
    name, the eight category titles/subtitles, each row's caption)."""
    return i18n_cache_key(_hist_base_key(year, month, by_main=by_main))


def invalidate_period_cache(year: int, month: int | None = None) -> None:
    """Drop cached historical boards for a period, in EVERY language. A month rebuild also
    busts the whole-year aggregate (year totals include that month).

    Same reasoning as ``stockpile.assets.invalidate_assets_cache``: the boards are cached per
    language but a rebuild changes the numbers in all of them, and the rebuild runs in a task
    under one arbitrary locale. Busting only that one would leave every other locale serving
    pre-rebuild rankings for the rest of the TTL. ``settings.LANGUAGES`` (the full framework
    set) is swept, not just the enabled locales, so a locale disabled after it was cached
    cannot leave an orphaned entry behind.

    ``kb:hist_years`` is a list of ints — no prose — so it keeps its single, unscoped key.
    """
    keys = []
    for code, _label in settings.LANGUAGES:
        with translation.override(code):
            for by_main in (False, True):  # KB-23: by-char + by-main variants both go stale
                keys.append(_hist_key(year, month, by_main=by_main))
                keys.append(_hist_key(year, None, by_main=by_main))  # year-total changes too
    keys.append(f"kb:hist_years:{CACHE_VERSION}:{_home()}")
    cache.delete_many(keys)


# --------------------------------------------------------------------------- #
#  Historical rankings (read path)
# --------------------------------------------------------------------------- #
def available_years(*, use_cache: bool = True) -> list[int]:
    """Years with any aggregated data, newest first (for the filter dropdown).

    Key left UNSCOPED on purpose: the payload is a list of integers. A year number reads the
    same in every locale, so language-keying it would just store nine identical lists.
    """
    key = f"kb:hist_years:{CACHE_VERSION}:{_home()}"
    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            return cached
    years = sorted(
        {r["year"] for r in MonthlyPilotKillStat.objects.order_by().values("year").distinct()},
        reverse=True,
    )
    # Fall back to the raw killmail range if the aggregate isn't built yet.
    if not years:
        earliest = _earliest_period()
        if earliest is not None:
            now = timezone.now().astimezone(_EVE_TZ)
            years = list(range(now.year, earliest[0] - 1, -1))
    if use_cache:
        cache.set(key, years, CACHE_TTL)
    return years


def _merge_period_pilots(year: int, month: int | None) -> list[dict]:
    """Per-pilot totals for a month (or a whole year, summed), from the aggregate."""
    qs = MonthlyPilotKillStat.objects.filter(year=year)
    if month:
        qs = qs.filter(month=month)
    rows = (
        qs.order_by()
        .values("character_id")
        .annotate(
            kills=Sum("kills"), losses=Sum("losses"), solo_kills=Sum("solo_kills"),
            final_blows=Sum("final_blows"), isk_destroyed=Sum("isk_destroyed"),
            isk_lost=Sum("isk_lost"), points=Sum("points"), active_days=Sum("active_days"),
        )
    )
    pilots = []
    for r in rows:
        kills = r["kills"] or 0
        losses = r["losses"] or 0
        isk_d = float(r["isk_destroyed"] or 0)
        isk_l = float(r["isk_lost"] or 0)
        denom = isk_d + isk_l
        pilots.append({
            "character_id": r["character_id"],
            "kills": kills,
            "losses": losses,
            "solo_kills": r["solo_kills"] or 0,
            "final_blows": r["final_blows"] or 0,
            "isk_destroyed": r["isk_destroyed"] or 0,
            "isk_lost": r["isk_lost"] or 0,
            "points": r["points"] or 0,
            "active_days": r["active_days"] or 0,
            "engagements": kills + losses,
            "efficiency": (isk_d / denom * 100.0) if denom else 0.0,
        })
    return pilots


def _period_label(year: int, month: int | None) -> str:
    """``"July 2026"`` (or just ``"2026"``), localised.

    NOT ``calendar.month_name`` — the C library's names are locked to the C locale and never
    translate. ``date_format(..., "F Y")`` uses Django's own translated month names and is
    byte-identical to the old output under English.
    """
    if not month:
        return str(year)
    return formats.date_format(datetime(year, month, 1, tzinfo=_EVE_TZ), "F Y")


def historical_leaderboards(
    year: int, month: int | None = None, *, use_cache: bool = True, refresh: bool = False,
    by_main: bool = False,
) -> dict:
    """The same eight boards as the live rankings, for a past calendar month or a
    whole year, read fast from ``MonthlyPilotKillStat``. Same payload shape as
    ``leaderboards.leaderboards`` so the template renders it unchanged.

    ``by_main=True`` rolls a person's alts up under their main (KB-23), cached separately.

    The boards and their category cards are built by the shared ``leaderboards`` helpers, so
    the prose (titles, subtitles, row captions) is marked and resolved in exactly one place.
    """
    from .leaderboards import (
        EFFICIENCY_MIN_FIGHTS,
        Window,
        _most_valuable_kills,
        _rollup_by_main,
        build_boards,
        categories_payload,
    )

    key = _hist_key(year, month, by_main=by_main)
    if use_cache and not refresh:
        cached = cache.get(key)
        if cached is not None:
            return cached

    pilots = _merge_period_pilots(year, month)
    if by_main:
        pilots = _rollup_by_main(pilots)
    categories = categories_payload(build_boards(pilots))

    # Biggest single kills in the period — small bounded query on the raw mails.
    start, _ = _period_bounds(year, month or 1)
    if month:
        _, end = _period_bounds(year, month)
    else:
        end = _period_bounds(year, 12)[1]
    window = Window(key=f"{year}-{month or 0}", label=_period_label(year, month), start=start, end=end)

    payload = {
        "window": {"key": f"y{year}m{month or 0}", "label": _period_label(year, month),
                   "year": year, "month": month, "historical": True},
        "categories": categories,
        "most_valuable": _most_valuable_kills(window),
        "pilot_count": len(pilots),
        "efficiency_min_fights": EFFICIENCY_MIN_FIGHTS,
    }
    if use_cache:
        cache.set(key, payload, CACHE_TTL)
    return payload
