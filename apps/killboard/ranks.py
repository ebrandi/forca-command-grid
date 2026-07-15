"""Combat rank ladder — the DB-driven source of truth + progression helpers.

The rank ladder used to be a hard-coded list in ``leaderboards.RANK_LADDER``. It
is now rows in :class:`~apps.killboard.models.CombatRankTitle` that leaders manage
from the Admin Console. ``combat_rank(kills)`` maps an all-time PvP kill count onto
the active ladder; ``rank_progress(kills)`` adds everything the pilot dashboard
needs (next rung, kills-to-go, a progress bar and the full ladder with earned flags).

The ladder is cached (small, read on every card render) and falls back to a static
default when the table is empty, so ranks keep working before the seed migration
runs or in a bare test database. It is PvP-kill based today; the ``metric`` column
leaves the door open to rank on other stats later without touching callers.
"""
from __future__ import annotations

from django.core.cache import cache
from django.utils.translation import gettext_lazy as _

from . import ranks_i18n

CACHE_VERSION = 1
LADDER_TTL = 600
_LADDER_KEY = f"kb:ranks:{CACHE_VERSION}:ladder"

# Static fallbacks (the pre-DB ladders), used ONLY when no active rank rows exist for a
# metric. (min threshold, title, tailwind colour class). KILLS is the original ladder;
# the support-role tracks (4.3) give the zero-kill logi/solo/regular pilot a rung to
# climb — thresholds scaled to those (smaller) counts. All reward-less by default.
_FALLBACK_LADDERS: dict[str, list[tuple[int, str, str]]] = {
    "kills": [
        (0, "Capsuleer", "text-faint"), (1, "Recruit", "text-muted"),
        (10, "Hunter", "text-cyan"), (50, "Killer", "text-gold"),
        (150, "Marauder", "text-gold"), (400, "Warlord", "text-kill"),
        (1000, "Apex Predator", "text-kill"),
    ],
    "solo_kills": [
        (0, "Wingman", "text-faint"), (1, "Lone Wolf", "text-muted"),
        (10, "Duelist", "text-cyan"), (30, "Solo Hunter", "text-gold"),
        (75, "Nomad", "text-gold"), (150, "Ghost", "text-kill"),
    ],
    "final_blows": [
        (0, "Trigger", "text-faint"), (5, "Finisher", "text-muted"),
        (25, "Executioner", "text-cyan"), (75, "Closer", "text-gold"),
        (200, "Reaper", "text-kill"),
    ],
    "active_days": [
        (0, "Visitor", "text-faint"), (5, "Regular", "text-muted"),
        (20, "Committed", "text-cyan"), (60, "Veteran", "text-gold"),
        (150, "Ever-Present", "text-gold"), (365, "Pillar", "text-kill"),
    ],
}


def _fallback_ladder(metric: str) -> list[dict]:
    rungs = _FALLBACK_LADDERS.get(metric) or _FALLBACK_LADDERS["kills"]
    return [
        {
            "id": None, "name": name, "min_kills": threshold, "color": color,
            "icon": "", "tier": i, "is_visible": True,
            "grants_reward": False, "reward_type": "none", "reward_amount": 0.0,
            "reward_item_type_id": None,
        }
        for i, (threshold, name, color) in enumerate(rungs)
    ]


def active_ladder(metric: str | None = None, *, use_cache: bool = True) -> list[dict]:
    """The active rank ladder for ``metric`` (default kills), ascending by threshold, cached.

    Each entry is a plain dict (cache-friendly) carrying everything the callers need:
    display fields, the tier index, and the reward config for that rung. Any metric with
    no active DB rows falls back to its static ladder, so the support-role tracks (4.3)
    work out-of-the-box before leadership customises them. The ``min_kills`` field is the
    generic threshold column, reused for whatever ``metric`` the ladder ranks on.
    """
    from .models import RankMetric

    metric = metric or RankMetric.KILLS
    key = f"{_LADDER_KEY}:{metric}"
    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            return cached

    from .models import CombatRankTitle

    rows = list(
        CombatRankTitle.objects.filter(is_active=True, metric=metric)
        .order_by("min_kills", "sort_order")
        .values(
            "id", "name", "min_kills", "color_class", "badge_icon", "is_visible",
            "grants_reward", "reward_type", "reward_amount", "reward_item_type_id",
        )
    )
    if not rows:
        ladder = _fallback_ladder(metric)
    else:
        ladder = [
            {
                "id": r["id"], "name": r["name"], "min_kills": r["min_kills"],
                "color": r["color_class"], "icon": r["badge_icon"], "tier": i,
                "is_visible": r["is_visible"], "grants_reward": r["grants_reward"],
                "reward_type": r["reward_type"], "reward_amount": float(r["reward_amount"] or 0),
                "reward_item_type_id": r["reward_item_type_id"],
            }
            for i, r in enumerate(rows)
        ]
    if use_cache:
        cache.set(key, ladder, LADDER_TTL)
    return ladder


# The support-role tracks surfaced alongside the primary KILLS rank (4.3): (metric,
# count key, display label). Kills stays the headline card; these give every playstyle
# a rung. Labels frame them positively — none ranks a pilot on absence.
_TRACK_METRICS = [
    ("solo_kills", "solo_kills", _("Solo kills")),
    ("final_blows", "final_blows", _("Final blows")),
    ("active_days", "active_days", _("Active days")),
]


def pilot_metric_counts(character_id: int) -> dict[str, int]:
    """A pilot's all-time counts for every rank metric (4.3).

    kills / solo_kills / final_blows come from the cached all-time CombatMetric card;
    active_days is summed from the per-month stat rows. Lazy imports keep this off the
    leaderboards↔ranks import cycle."""
    from django.db.models import Sum

    from apps.killboard.leaderboards import pilot_combat_card

    from .models import MonthlyPilotKillStat

    card = pilot_combat_card(character_id)
    active = (
        MonthlyPilotKillStat.objects.filter(character_id=character_id)
        .aggregate(d=Sum("active_days"))["d"]
        or 0
    )
    return {
        "kills": int(card.get("kills", 0) or 0),
        "solo_kills": int(card.get("solo_kills", 0) or 0),
        "final_blows": int(card.get("final_blows", 0) or 0),
        "active_days": int(active),
    }


def pilot_track_standings(counts: dict[str, int]) -> list[dict]:
    """Each support-role rank track's standing for a pilot, given their metric counts.

    Reuses :func:`rank_progress` (metric-agnostic) per track. Returns display-ready dicts
    with neutral key names so the template isn't kill-centric. Display-only — these ladders
    carry no rewards (the reward engine ranks on kills alone)."""
    out: list[dict] = []
    for metric, count_key, label in _TRACK_METRICS:
        ladder = active_ladder(metric)
        count = int(counts.get(count_key, 0) or 0)
        prog = rank_progress(count, ladder)
        nxt = prog["next"]
        out.append({
            "metric": metric, "label": label, "count": count,
            "current": prog["current"], "next": nxt,
            "to_next": prog["kills_to_next"], "progress_pct": prog["progress_pct"],
            "is_maxed": prog["is_maxed"], "tier": prog["tier"], "max_tier": prog["max_tier"],
        })
    return out


def invalidate_ladder_cache() -> None:
    """Drop every metric's cached ladder after a leader edits the rank config."""
    from .models import RankMetric

    cache.delete(_LADDER_KEY)  # legacy un-suffixed key, if any
    for metric in RankMetric.values:
        cache.delete(f"{_LADDER_KEY}:{metric}")


def _reward_configured(entry: dict) -> bool:
    """Whether a ladder rung actually carries a payable reward (mirrors
    ``CombatRankTitle.rewards_configured`` over the cache-friendly dict)."""
    rt = entry.get("reward_type") or "none"
    if not entry.get("grants_reward") or rt == "none":
        return False
    if rt == "item":
        return bool(entry.get("reward_item_type_id"))
    if rt == "manual":
        return True
    return (entry.get("reward_amount") or 0) > 0


def combat_rank(kills: int, ladder: list[dict] | None = None) -> dict:
    """Map an all-time PvP kill count to a rank title + colour.

    Backward-compatible with the old ``leaderboards.combat_rank``: returns at least
    ``{title, color, tier, max_tier}``. Pass a pre-fetched ``ladder`` to avoid a
    cache read per call in hot loops (e.g. the corp roster).
    """
    ladder = ladder if ladder is not None else active_ladder()
    if not ladder:  # pragma: no cover - active_ladder always returns the fallback
        return {"title": ranks_i18n.rank_title_for("Capsuleer"), "color": "text-faint",
                "tier": 0, "max_tier": 0, "min_kills": 0, "icon": "", "name": "Capsuleer"}
    current = ladder[0]
    for entry in ladder:
        if kills >= entry["min_kills"]:
            current = entry
        else:
            break
    return {
        # ``title`` is the rendered label (translated seed until a leader renames the rank);
        # ``name`` stays the RAW English so write/audit paths (rank_notify, reward snapshots)
        # never freeze a reader's locale into a stored row.
        "title": ranks_i18n.rank_title_for(current["name"]),
        "name": current["name"],
        "color": current["color"],
        "icon": current["icon"],
        "tier": current["tier"],
        "min_kills": current["min_kills"],
        "max_tier": len(ladder) - 1,
    }


def rank_progress(kills: int, ladder: list[dict] | None = None) -> dict:
    """Everything the dashboard rank-progress card needs.

    Returns the current rung, the next rung (or ``None`` if maxed), kills remaining,
    a 0–100 progress percentage within the current band, and the full visible ladder
    annotated with earned/current flags for the progression view.
    """
    ladder = ladder if ladder is not None else active_ladder()
    cur = combat_rank(kills, ladder)
    tier = cur["tier"]

    nxt = ladder[tier + 1] if tier + 1 < len(ladder) else None
    if nxt is not None:
        span = max(1, nxt["min_kills"] - cur["min_kills"])
        done = max(0, kills - cur["min_kills"])
        progress_pct = min(100.0, round(done / span * 100.0, 1))
        kills_to_next = max(0, nxt["min_kills"] - kills)
    else:
        progress_pct = 100.0
        kills_to_next = 0

    visible = [
        {
            "name": ranks_i18n.rank_title_for(e["name"]), "min_kills": e["min_kills"],
            "color": e["color"], "icon": e["icon"], "tier": e["tier"],
            "earned": kills >= e["min_kills"],
            "is_current": e["tier"] == tier,
            "grants_reward": e["grants_reward"], "reward_type": e["reward_type"],
            "reward_amount": e["reward_amount"], "reward_item_type_id": e["reward_item_type_id"],
        }
        for e in ladder if e["is_visible"]
    ]

    # The nearest still-to-earn rung that actually carries a reward — looked up past
    # reward-less rungs so the pilot always sees what's coming next, not just whether the
    # very next title happens to grant one. Only visible rungs, so a hidden rung's name
    # never leaks. ``None`` when no reward-bearing rung lies ahead.
    next_reward = None
    for e in ladder:
        if e["tier"] > tier and e.get("is_visible", True) and _reward_configured(e):
            next_reward = {
                "title": ranks_i18n.rank_title_for(e["name"]), "name": e["name"],
                "min_kills": e["min_kills"],
                "color": e["color"], "reward_type": e["reward_type"],
                "reward_amount": e["reward_amount"], "reward_item_type_id": e["reward_item_type_id"],
                "kills_away": max(0, e["min_kills"] - kills),
            }
            break

    return {
        "kills": kills,
        "current": cur,
        "next": (
            {
                "title": ranks_i18n.rank_title_for(nxt["name"]), "name": nxt["name"],
                "min_kills": nxt["min_kills"],
                "color": nxt["color"], "icon": nxt["icon"], "grants_reward": nxt["grants_reward"],
                "reward_type": nxt["reward_type"], "reward_amount": nxt["reward_amount"],
                "reward_item_type_id": nxt["reward_item_type_id"],
            }
            if nxt else None
        ),
        "kills_to_next": kills_to_next,
        "progress_pct": progress_pct,
        "tier": tier,
        "max_tier": len(ladder) - 1,
        "ladder": visible,
        "is_maxed": nxt is None,
        "next_reward": next_reward,
    }
