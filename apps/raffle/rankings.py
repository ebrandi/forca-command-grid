"""Killboard standings scoped to a contest's own accrual window and its eligible pilots.

Fixed (rank-based) raffle prizes are awarded on where a pilot finished on a killboard
board — top killer, top solo, most active — over exactly ``[contest.start_at, contest.end_at)``.
That window is arbitrary, and :func:`apps.killboard.leaderboards.leaderboards` only accepts
its own fixed keys (``7d``/``30d``/``month``/…) and silently clamps anything else to 30d, so
this module builds boards directly from the same internals with a
:class:`~apps.killboard.leaderboards.Window` of its own. ``apps.killboard.aggregation`` already
does the same thing for historical months, so this is an established pattern, not a new one.

**These are not the killboard's boards.** Two deliberate differences:

* **Only contest-eligible accounts are ranked.** A pilot who never enrolled, or whose ESI
  token has lapsed, cannot win anything — so they are not on the board at all, and the
  places are computed among the pilots who are actually competing. That means the raffle's
  "top killer" can differ from the killboard's, which is correct: the raffle only knows
  about pilots who connected a token. Position #1 here is the pilot who gets paid.
* **Alts roll up under their main** before ranking, so one person cannot occupy two places
  and take a prize twice for the same kills.

All eight boards are built in one pass and cached together: merging kills and losses across
a month-long window is not something to repeat per category, nor to do on a page request on
four cores and spinning disks.
"""
from __future__ import annotations

from django.core.cache import cache

from apps.killboard import leaderboards as lb

from .models import RaffleContest

# The board keys a rank prize may be attached to, in the order leadership sees them.
# Sourced from the killboard's own CATEGORIES so the two can never drift apart.
CATEGORY_KEYS: list[str] = [key for (key, *_rest) in lb.CATEGORIES]
CATEGORY_LABELS: dict[str, str] = {key: title for (key, title, *_rest) in lb.CATEGORIES}
CATEGORY_SUBTITLES: dict[str, str] = {key: sub for (key, _t, sub, *_rest) in lb.CATEGORIES}
CATEGORY_KINDS: dict[str, str] = {key: kind for (key, _t, _s, kind, *_rest) in lb.CATEGORIES}
CATEGORY_ICONS: dict[str, str] = {key: icon for (key, _t, _s, _k, icon) in lb.CATEGORIES}

CATEGORY_CHOICES = [(key, CATEGORY_LABELS[key]) for key in CATEGORY_KEYS]

# Boards a fixed prize may be attached to. Deliberately NOT all eight — two of the
# killboard's boards are actively perverse as payouts:
#
# * ``isk_lost`` is the "bravest feeder" board. Paying ISK for losing ISK puts a bounty on
#   feeding, and the more expensive the loss the bigger the prize.
# * ``efficiency`` ranks ISK destroyed against ISK lost with a five-fight minimum, so a
#   pilot sitting on five kills and no losses is already at 100% and their best play for
#   the rest of the contest is to stop undocking. A PVP-event prize that pays pilots to
#   stay docked is the exact opposite of what the event is for.
#
# ``most_active`` is kept, but it counts days a pilot appeared on a killmail — kill OR
# loss — so the public rules must say that rather than let pilots infer "days of kills".
AWARDABLE_BOARDS: list[str] = [
    "top_killers", "isk_destroyed", "points", "final_blows", "solo_kills", "most_active",
]
AWARDABLE_CHOICES = [(key, CATEGORY_LABELS[key]) for key in AWARDABLE_BOARDS]

# How many places a board SHOWS. Every ranked pilot is still placed and kept — a pilot
# sitting 14th has to be able to see that they are 14th and what closing one place costs,
# which is the whole point of a standings board that is supposed to pull people up it.
BOARD_LIMIT = 10

# How deep the board is RANKED (as opposed to shown). Bounded so a huge corp cannot put an
# unbounded list into a cache entry, but far past any plausible prize position.
RANK_DEPTH = 200

_CACHE_VERSION = 2
_LIVE_TTL = 300     # accruing: standings move, but not every second
_FROZEN_TTL = 3600  # closed/completed: the window is in the past and cannot change


def is_valid_category(key: str) -> bool:
    return key in CATEGORY_LABELS


def contest_window(contest) -> lb.Window:
    """The killboard window matching a contest's accrual period exactly."""
    return lb.Window(
        key=f"contest:{contest.pk}",
        label=contest.name,
        start=contest.start_at,
        end=contest.end_at,
    )


def _cache_key(contest) -> str:
    # end_at is in the key so an edited time box can never serve stale standings.
    stamp = contest.end_at.isoformat() if contest.end_at else "-"
    return f"raffle:standings:{_CACHE_VERSION}:{contest.pk}:{stamp}"


def _as_number(value) -> float:
    """Board values arrive as int, Decimal (ISK) or float (efficiency %)."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _character_names(character_ids) -> dict[int, str]:
    from apps.sso.models import EveCharacter

    known = dict(
        EveCharacter.objects.filter(character_id__in=character_ids)
        .values_list("character_id", "name")
    )
    return {cid: known.get(cid) or str(cid) for cid in character_ids}


def _eligible_accounts(contest, character_ids) -> dict[int, int]:
    """``character_id -> user_id`` for characters whose account may win in this contest."""
    from apps.sso.models import EveCharacter

    from . import eligibility as elig

    accounts = dict(
        EveCharacter.objects.filter(character_id__in=character_ids, user__isnull=False)
        .values_list("character_id", "user_id")
    )
    if not accounts:
        return {}
    bulk = elig.for_users_bulk(contest, list(set(accounts.values())))
    return {
        cid: uid for cid, uid in accounts.items()
        if (bulk.get(uid) is not None and bulk[uid].eligible)
    }


# The per-character stat fields that sum when a person's characters are combined.
_ADDITIVE = ("kills", "final_blows", "solo_kills", "isk_destroyed",
             "points", "losses", "isk_lost", "active_days")


def _rollup_by_account(pilots, char_to_user: dict[int, int]) -> list[dict]:
    """Collapse per-character rows into one row per ACCOUNT.

    Deliberately not :func:`apps.killboard.leaderboards._rollup_by_main`. That keys on
    ``core.pilots.mains_for``, which maps a linked character whose account has **no main
    flagged** to itself — so an account with two characters and no main survives as two
    rows. On a display board that is a cosmetic duplicate; here it would let one person
    hold two places on the same board and collect the same fixed prize twice. Keying on
    ``user_id`` cannot do that, because the account is the thing being paid.

    The display character is the account's main when it has one, else its lowest character
    id — fixed, so the board does not rename itself between builds.
    """
    merged: dict[int, dict] = {}
    for p in pilots:
        uid = char_to_user.get(p["character_id"])
        if uid is None:
            continue
        row = merged.get(uid)
        if row is None:
            merged[uid] = {**p, "user_id": uid, "_chars": [p["character_id"]]}
            continue
        for field in _ADDITIVE:
            row[field] = row.get(field, 0) + p.get(field, 0)
        row["_chars"].append(p["character_id"])

    if merged:
        from apps.sso.models import EveCharacter

        mains = dict(
            EveCharacter.objects.filter(user_id__in=list(merged), is_main=True)
            .values_list("user_id", "character_id")
        )
        for uid, row in merged.items():
            row["character_id"] = mains.get(uid) or min(row.pop("_chars"))
            row.pop("_chars", None)
            row["engagements"] = row.get("kills", 0) + row.get("losses", 0)
            denom = float(row.get("isk_destroyed", 0)) + float(row.get("isk_lost", 0))
            row["efficiency"] = (
                float(row.get("isk_destroyed", 0)) / denom * 100.0) if denom else 0.0
    return list(merged.values())


def _build_all(contest) -> dict[str, dict]:
    """Every category's board for the contest window, eligible accounts only.

    Filtering happens BEFORE ranking, not after. Filtering a finished top-10 would leave a
    board of two rows whenever most of the corp's best killers had not enrolled; ranking the
    eligible pilots instead gives a full board of the people actually competing.

    Places are re-derived with an explicit tie-break rather than trusting the order the
    killboard returns: ``leaderboards._rank`` sorts with Python's stable sort over a GROUP BY
    result carrying no ORDER BY, so two pilots on identical numbers can swap places between
    runs. Harmless on a display board, unacceptable when the position decides who gets paid.
    Ties break on the lower character id — arbitrary, but fixed, so re-running an award
    reproduces the same winner.

    EVERY ranked pilot is placed and kept in ``ranked``; ``rows`` is only what the page
    shows. A pilot outside the top ten still needs to see where they stand and what one
    place costs, or the board cannot do the job it exists for.
    """
    from django.utils import timezone

    window = contest_window(contest)
    stamp = timezone.now().isoformat()
    empty = {key: {"rows": [], "ranked": [], "as_of": stamp} for key in CATEGORY_KEYS}

    pilots = list(lb._merge_pilots(window).values())
    if not pilots:
        return empty

    # Resolve accounts BEFORE rolling up: the account is both the eligibility unit and the
    # rollup key, so doing it the other way round would roll up by the wrong thing.
    eligible = _eligible_accounts(contest, [p["character_id"] for p in pilots])
    pilots = _rollup_by_account(
        [p for p in pilots if p["character_id"] in eligible], eligible)
    if not pilots:
        return empty
    boards = lb.build_boards(pilots, limit=RANK_DEPTH)
    names = _character_names([p["character_id"] for p in pilots])
    owners = {p["character_id"]: p["user_id"] for p in pilots}

    out: dict[str, dict] = {}
    for key in CATEGORY_KEYS:
        rows = [dict(r) for r in boards.get(key, [])]
        rows.sort(key=lambda r: (-_as_number(r.get("value")), r["character_id"]))
        for i, row in enumerate(rows, start=1):
            cid = row["character_id"]
            row["place"] = i
            row["name"] = names.get(cid, str(cid))
            row["user_id"] = owners.get(cid)
            # Values are cached (and later frozen into a JSONField); Decimal and float do
            # not survive that round trip cleanly, so normalise once, here.
            row["value"] = _as_number(row.get("value"))
        out[key] = {"rows": rows[:BOARD_LIMIT], "ranked": rows, "as_of": stamp}
    return out


def all_standings(contest, *, use_cache: bool = True) -> dict[str, dict]:
    """Every board for the contest, cached as one payload (one expensive pass, eight boards).

    Building these merges every kill and loss across the whole contest window. Each contest
    is its own cache key, so the killboard's own memo buys nothing here — this must not run
    on a page request if it can be helped. ``apps.raffle.tasks.refresh_adoption`` warms it.
    """
    key = _cache_key(contest)
    if use_cache:
        hit = cache.get(key)
        if hit is not None:
            return hit
    boards = _build_all(contest)
    ttl = _FROZEN_TTL if contest.status in (
        RaffleContest.Status.CLOSED, RaffleContest.Status.COMPLETED,
        RaffleContest.Status.ARCHIVED,
    ) else _LIVE_TTL
    cache.set(key, boards, ttl)
    return boards


def board(contest, category: str, *, use_cache: bool = True) -> dict:
    """One category's full payload: ``rows`` (displayed), ``ranked`` (all), ``as_of``."""
    if not is_valid_category(category):
        return {"rows": [], "ranked": [], "as_of": None}
    return all_standings(contest, use_cache=use_cache).get(
        category, {"rows": [], "ranked": [], "as_of": None})


def standings(contest, category: str, *, use_cache: bool = True) -> list[dict]:
    """One category's displayed board: ``place``, ``character_id``, ``name``, ``value``,
    ``secondary``, ``user_id``. Every row is a pilot who could actually win."""
    return board(contest, category, use_cache=use_cache)["rows"]


def invalidate(contest) -> None:
    """Drop cached standings (called when the contest's time box or roster changes)."""
    cache.delete(_cache_key(contest))


def pilot_standing(contest, category: str, user) -> dict | None:
    """Where ``user`` sits on this board, and what it would take to climb one place.

    Answers for ANY ranked pilot, not just the ten on screen — being 14th and seeing what
    13th costs is exactly the nudge this board exists to give. Returns None only when the
    pilot has scored nothing in this category, which the template turns into "get on a kill
    to enter" rather than a position they do not hold.

    ``tied_ahead`` matters: places are broken on character id, so the pilot above can be on
    the *same* number. Reporting a gap of zero as "you need 0 more" reads as a bug — the
    template needs to say "you are level with them; one more puts you ahead".
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    data = board(contest, category)
    ranked = data.get("ranked") or []
    mine = next((r for r in ranked if r.get("user_id") == user.pk), None)
    if mine is None:
        return None
    ahead = next((r for r in ranked if r["place"] == mine["place"] - 1), None)
    gap = (ahead["value"] - mine["value"]) if ahead else None
    return {
        "place": mine["place"],
        "value": mine["value"],
        "secondary": mine.get("secondary"),
        "kind": CATEGORY_KINDS.get(category, "int"),
        "ranked_pilots": len(ranked),
        "on_display_board": mine["place"] <= BOARD_LIMIT,
        "ahead_place": ahead["place"] if ahead else None,
        "ahead_name": ahead["name"] if ahead else None,
        "ahead_value": ahead["value"] if ahead else None,
        # The honest distance to the next place up, in that board's own units.
        "gap": gap,
        "tied_ahead": bool(ahead is not None and gap == 0),
        "is_leader": mine["place"] == 1,
        "as_of": data.get("as_of"),
    }


def winner_for(contest, category: str, position: int, *, ranked_rows=None) -> dict | None:
    """The account that takes the prize for ``position``, or None if nobody holds it.

    Trivial by construction: the board already contains only pilots who can win, so the
    winner of position N is simply the pilot at place N. Nobody is "passed over", which is
    exactly what makes the board a pilot watched all month the same board that paid out.

    Note what is deliberately NOT excluded: a pilot who already took another rank prize.
    Fixed prizes are earned, not drawn — topping both the kill board and the solo board is
    two achievements and pays twice, and the same pilot may still win a ticket prize on top.
    Only the ticket draw limits a pilot to one prize, because that one is luck.
    """
    rows = ranked_rows if ranked_rows is not None else board(
        contest, category, use_cache=False)["ranked"]
    return next((r for r in rows if r["place"] == position), None)
