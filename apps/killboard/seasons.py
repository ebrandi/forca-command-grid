"""KB-37 (WS-D3) — seasonal (quarterly) ladders.

A "season" is an ISO/calendar quarter. Because ``MonthlyPilotKillStat`` holds one row per pilot
per calendar month and the three months of a quarter partition the calendar with **no overlap**,
a quarter's boards compose *exactly* by summing those monthly rows — every additive column
(kills, losses, solo, final blows, ISK, points, active days) adds cleanly, and the derived
efficiency is recomputed. So the same eight leaderboards the live rankings show are reproduced for
any past quarter straight from the aggregate (no killmail re-scan). Completed quarters are frozen
into :class:`SeasonSnapshot`; the in-progress quarter is computed live from the same helper.
"""
from __future__ import annotations

import datetime as dt

from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import MonthlyPilotKillStat, SeasonSnapshot

QUARTER_MONTHS = {1: (1, 2, 3), 2: (4, 5, 6), 3: (7, 8, 9), 4: (10, 11, 12)}
TOP_N = 3  # a season page shows the podium (top-3) per board
# The board keys produced by leaderboards.build_boards, in display order, with their labels.
BOARD_KEYS = [
    "top_killers", "isk_destroyed", "points", "final_blows",
    "solo_kills", "most_active", "isk_lost", "efficiency",
]
BOARD_LABELS = {
    "top_killers": _("Top Killers"), "isk_destroyed": _("Most ISK Destroyed"),
    "points": _("Top Points"), "final_blows": _("Final Blows"), "solo_kills": _("Solo Kills"),
    "most_active": _("Most Active"), "isk_lost": _("Most ISK Lost"), "efficiency": _("Best Efficiency"),
}


def board_label(key: str) -> str:
    return str(BOARD_LABELS.get(key, key))


def quarter_of(date: dt.date) -> tuple[int, int]:
    return date.year, (date.month - 1) // 3 + 1


def current_quarter() -> tuple[int, int]:
    return quarter_of(timezone.now().date())


def _merge_quarter_pilots(year: int, quarter: int) -> list[dict]:
    """Per-pilot totals for a quarter (its three months summed), shaped for ``build_boards``."""
    rows = (
        MonthlyPilotKillStat.objects.filter(year=year, month__in=QUARTER_MONTHS[quarter])
        .order_by()
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
            "kills": kills, "losses": losses,
            "solo_kills": r["solo_kills"] or 0, "final_blows": r["final_blows"] or 0,
            "isk_destroyed": r["isk_destroyed"] or 0, "isk_lost": r["isk_lost"] or 0,
            "points": r["points"] or 0, "active_days": r["active_days"] or 0,
            "engagements": kills + losses,
            "efficiency": (isk_d / denom * 100.0) if denom else 0.0,
        })
    return pilots


def compute_boards(year: int, quarter: int, *, top_n: int = TOP_N) -> dict:
    """The eight boards' top-``top_n`` placements for a quarter — locale-neutral rows only.

    Each row is ``{"place", "character_id", "value"}`` (no prose), so a persisted snapshot is
    language-independent; captions/labels are rendered from the board key at read time.
    """
    return _boards_from_pilots(_merge_quarter_pilots(year, quarter), top_n=top_n)


def _boards_from_pilots(pilots: list[dict], *, top_n: int) -> dict:
    from .leaderboards import build_boards

    boards = build_boards(pilots)
    out = {}
    for key in BOARD_KEYS:
        out[key] = [
            {"place": row["place"], "character_id": row["character_id"], "value": row["value"]}
            for row in boards.get(key, [])[:top_n]
        ]
    return out


def snapshot_season(year: int, quarter: int) -> SeasonSnapshot:
    """Compute + persist (upsert) a season's boards. Idempotent."""
    boards = compute_boards(year, quarter)
    pilot_count = len(_merge_quarter_pilots(year, quarter))
    snap, _created = SeasonSnapshot.objects.update_or_create(
        year=year, quarter=quarter,
        defaults={"boards": boards, "pilot_count": pilot_count},
    )
    return snap


def _has_data(year: int, quarter: int) -> bool:
    return MonthlyPilotKillStat.objects.filter(
        year=year, month__in=QUARTER_MONTHS[quarter]
    ).exists()


def snapshot_completed_seasons(*, lookback_quarters: int = 8) -> int:
    """Freeze any recently-completed quarter that has data but no snapshot yet.

    Bounded to the last ``lookback_quarters`` completed quarters, so the beat is cheap and never
    re-scans deep history. Returns the number of snapshots written/refreshed.
    """
    cy, cq = current_quarter()
    written = 0
    y, q = cy, cq
    for _i in range(lookback_quarters):
        # step back one quarter
        q -= 1
        if q == 0:
            q, y = 4, y - 1
        if not _has_data(y, q):
            continue
        snapshot_season(y, q)  # upsert (refreshes if late killmails landed)
        written += 1
    return written


def season_payload(year: int, quarter: int) -> dict:
    """One season for the page: the persisted snapshot if present, else a live computation."""
    snap = SeasonSnapshot.objects.filter(year=year, quarter=quarter).first()
    cy, cq = current_quarter()
    is_current = (year, quarter) == (cy, cq)
    if snap and not is_current:
        boards = snap.boards
        pilot_count = snap.pilot_count
    else:
        boards = compute_boards(year, quarter)
        pilot_count = len(_merge_quarter_pilots(year, quarter))
    return {
        "year": year, "quarter": quarter, "is_current": is_current,
        "boards": boards, "pilot_count": pilot_count,
    }


def available_seasons() -> list[tuple[int, int]]:
    """(year, quarter) pairs that have data, newest first — for the season index."""
    pairs: set[tuple[int, int]] = set()
    for year, month in (
        MonthlyPilotKillStat.objects.order_by().values_list("year", "month").distinct()
    ):
        pairs.add((year, (month - 1) // 3 + 1))
    return sorted(pairs, reverse=True)
