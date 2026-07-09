"""Hall of Fame — the corp contribution scoreboard, by month.

Ranks **pilots (characters)**, not just registered app users, so the whole corp
shows up. Combines three sources into one comparable points scale (current
weights):

* the **contribution ledger** (build/haul/task/srp/mining/fleet/train/doctrine) —
  the stored per-event points, attributed to the member's main character;
* **PVP** — every kill a corp pilot was an attacker on (``KillmailParticipant``
  where ``corporation_id`` is the home corp — all kills, not just final blows);
* **PVE** — the corp's ratting/bounty income attributed to the ratting pilot
  (``CorpWalletJournalEntry`` ``first_party_id`` by ref_type).

Computed on read (no materialised rows) so past months come straight from the
data and weight changes show up immediately. Results are cached per month. Pilot
names resolve from our own characters first, then the EveName cache (so enemy-
slaying alts with no app account still get a name).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from django.core.cache import cache
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.pilots.models import ContributionEvent
from apps.pilots.weights import points_for

_CACHE_TTL = 300
_MAX_MONTHS = 24
OVERALL_TOP = 10
CATEGORY_TOP = 5

# Ordered categories shown on the page (ledger kinds + the two derived ones).
CATEGORIES: list[tuple[str, str]] = [
    *ContributionEvent.Kind.choices,
    ("pvp", "PvP kills"),
    ("pve", "Ratting income"),
]
CATEGORY_LABELS = dict(CATEGORIES)


def month_range(year: int, month: int):
    """Tz-aware [start, end) for a calendar month."""
    start = timezone.make_aware(datetime(year, month, 1))
    end = (
        timezone.make_aware(datetime(year + 1, 1, 1))
        if month == 12
        else timezone.make_aware(datetime(year, month + 1, 1))
    )
    return start, end


def available_months() -> list[dict]:
    """Months we have any data for, newest first (capped)."""
    from apps.killboard.models import Killmail

    now = timezone.now()
    earliest = now
    km = Killmail.objects.order_by("killmail_time").values_list("killmail_time", flat=True).first()
    ce = ContributionEvent.objects.order_by("occurred_at").values_list("occurred_at", flat=True).first()
    for ts in (km, ce):
        if ts and ts < earliest:
            earliest = ts

    months: list[dict] = []
    y, m = now.year, now.month
    for _ in range(_MAX_MONTHS):
        start, _end = month_range(y, m)
        months.append({"year": y, "month": m, "key": f"{y:04d}-{m:02d}", "label": f"{start:%B %Y}"})
        if start <= earliest:
            break
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return months


def _is_current_month(year: int, month: int) -> bool:
    now = timezone.now()
    return year == now.year and month == now.month


def _not_yet_completed(year: int, month: int) -> bool:
    """True for the current month or any FUTURE month — a month that hasn't finished
    being scored and so must never be frozen (freezing a future month would forward-date
    a stale snapshot that later wins over the real weights — review L1)."""
    now = timezone.now()
    return (year, month) >= (now.year, now.month)


def _freeze_month(year: int, month: int, weights=None):
    """Snapshot the given (or current) weights for a completed month and return a
    weights object built from the snapshot. Idempotent (get_or_create on year+month).
    A not-yet-completed month is never persisted — it tracks live weights."""
    from apps.pilots.models import MonthlyWeightSnapshot
    from apps.pilots.weights import active_weights, weights_from_snapshot, weights_snapshot_dict

    w = weights or active_weights()
    if _not_yet_completed(year, month):
        return w
    snap, _ = MonthlyWeightSnapshot.objects.get_or_create(
        year=year, month=month, defaults={"weights": weights_snapshot_dict(w)}
    )
    return weights_from_snapshot(snap.weights)


def weights_for_month(year: int, month: int):
    """The weights the given month's Hall of Fame is scored with (4.15).

    The in-progress (current) — or any future — month always uses the live weights: it is
    still accruing and reflects current policy, and must never be frozen. A completed
    month uses its frozen snapshot; if none exists yet (e.g. before the daily freeze /
    save-hook ran), it is frozen NOW at the current weights (which, absent a weight change
    since it closed, are the ones that were active during it) so it never shifts again.
    """
    from apps.pilots.models import MonthlyWeightSnapshot
    from apps.pilots.weights import active_weights, weights_from_snapshot

    if _not_yet_completed(year, month):
        return active_weights()
    snap = MonthlyWeightSnapshot.objects.filter(year=year, month=month).first()
    if snap is not None:
        return weights_from_snapshot(snap.weights)
    return _freeze_month(year, month)


def freeze_completed_months(weights=None) -> int:
    """Freeze every completed month that lacks a snapshot, at the given (or current)
    weights. Call this **before** applying a live-weights change so history is captured
    at the pre-change values; also run daily as a safety net. Returns rows created."""
    from apps.pilots.models import MonthlyWeightSnapshot
    from apps.pilots.weights import active_weights, weights_snapshot_dict

    data = weights_snapshot_dict(weights or active_weights())
    frozen = 0
    for m in available_months():
        if _is_current_month(m["year"], m["month"]):
            continue  # the in-progress month tracks live weights, never frozen
        _, created = MonthlyWeightSnapshot.objects.get_or_create(
            year=m["year"], month=m["month"], defaults={"weights": dict(data)}
        )
        frozen += 1 if created else 0
    return frozen


def _user_main_char() -> dict[int, int]:
    """Registered user_id → their main character_id (any character as fallback)."""
    from apps.sso.models import EveCharacter

    out: dict[int, int] = {}
    for cid, uid in (
        EveCharacter.objects.filter(user__isnull=False)
        .order_by("-is_main", "character_id")
        .values_list("character_id", "user_id")
    ):
        out.setdefault(uid, cid)
    return out


def _ledger_points(start, end, user_main, scores: dict) -> None:
    # 'mining' is scored from the raw mining ledger (like pvp/pve from their
    # sources), so exclude the payout-based mining events here to avoid double.
    rows = (
        ContributionEvent.objects.filter(occurred_at__gte=start, occurred_at__lt=end)
        .exclude(kind="mining")
        .values("user_id", "kind")
        .annotate(pts=Sum("points"))
    )
    for r in rows:
        cid = user_main.get(r["user_id"])
        if cid and r["pts"]:
            scores[cid][r["kind"]] += int(r["pts"])


def _mining_points(start, end, weights, scores: dict, name_hints: dict) -> None:
    """Mining from the corp mining LEDGER (observer data), valued at Jita — so it
    counts even when no payout has been processed. Mirrors pvp/pve being derived
    from their raw ESI sources."""
    from datetime import timedelta

    from apps.mining.services import participation

    last_day = (end - timedelta(days=1)).date()
    for p in participation(start.date(), last_day):
        cid = p["character_id"]
        pts = points_for("mining", magnitude=float(p["value"]), weights=weights)
        if pts:
            scores[cid]["mining"] += pts
        if p.get("name"):
            name_hints[cid] = p["name"]


def _pvp_points(start, end, home_corp, weights, scores: dict) -> None:
    if not home_corp:
        return
    from apps.killboard.models import KillmailParticipant

    rows = (
        KillmailParticipant.objects.filter(
            role="attacker",
            corporation_id=home_corp,
            killmail__killmail_time__gte=start,
            killmail__killmail_time__lt=end,
        )
        .values("character_id")
        .annotate(kills=Count("id"), fb=Count("id", filter=Q(final_blow=True)))
    )
    for r in rows:
        cid = r["character_id"]
        if not cid:
            continue
        pts = points_for("pvp", magnitude=r["kills"], final_blows=r["fb"], weights=weights)
        if pts:
            scores[cid]["pvp"] += pts


def _pve_points(start, end, weights, scores: dict) -> None:
    from apps.corporation.models import CorpWalletJournalEntry

    ref_types = weights.pve_ref_type_list()
    if not ref_types:
        return
    # For ratting income (bounty_prizes / ess_escrow_transfer) the corp's cut is
    # journalled with the NPC payer (CONCORD/ESS) as first_party and the *member*
    # who earned it as second_party — so we attribute by second_party_id.
    rows = (
        CorpWalletJournalEntry.objects.filter(
            ref_type__in=ref_types, amount__gt=0, date__gte=start, date__lt=end
        )
        .exclude(second_party_id=None)
        .values("second_party_id")
        .annotate(isk=Sum("amount"))
    )
    for r in rows:
        cid = r["second_party_id"]
        pts = points_for("pve", magnitude=float(r["isk"] or 0), weights=weights)
        if pts:
            scores[cid]["pve"] += pts


def _resolve_names(char_ids: set[int], hints: dict | None = None) -> dict[int, str]:
    """character_id → name: our own characters, then the EveName cache, then any
    hints (e.g. names carried on the mining ledger)."""
    from apps.corporation.models import EveName
    from apps.sso.models import EveCharacter

    hints = hints or {}
    out: dict[int, str] = {}
    for cid, name in (
        EveCharacter.objects.filter(character_id__in=char_ids).values_list("character_id", "name")
    ):
        out[cid] = name or f"#{cid}"
    missing = [c for c in char_ids if c not in out]
    if missing:
        for eid, name in EveName.objects.filter(entity_id__in=missing).values_list("entity_id", "name"):
            out[eid] = name
    for cid in char_ids:
        if cid not in out and cid in hints:
            out[cid] = hints[cid]
    return out


def category_how(key: str, w) -> str:
    """One-line description of how a category scores, under the current weights."""
    how = {
        "pvp": f"{w.pvp_points_per_kill} pt/kill"
               + (f" +{w.pvp_final_blow_bonus} final blow" if w.pvp_final_blow_bonus else ""),
        "pve": f"{w.pve_points_per_mil} pts / 1M ISK ratting income",
        "mining": f"{w.mining_points_per_mil} pts / 1M ISK mined (Jita value)",
        "build": f"{w.build_points_per_ship} pt/ship built",
        "haul": f"{w.haul_points} pts/delivery"
                + (" (ESI-verified)" if w.haul_requires_verification else ""),
        "task": f"{w.task_points} pt/task done",
        "srp": f"{w.srp_points_per_mil} pts / 1M ISK",
        "fleet": f"{w.fleet_points} pts/fleet attended",
        "train": f"{w.train_points_per_level} pt/recommended skill level",
        "doctrine": f"{w.doctrine_base} base + corp priority & required SP",
        # Directive credit is the directive's own configured points (stored on the
        # ledger event), not a per-unit weight — so it has no weight formula here.
        "directive": "the directive's own points",
    }
    return how.get(key, "")


def _drop_opted_out(scores: dict) -> None:
    """Remove every character of a recognition-opted-out pilot from the scores.

    ``PilotPreference.public_recognition=False`` is honoured by the recognition feed
    and points leaderboard; the Hall of Fame must honour it too (the ``/privacy/``
    page and the contributions toggle promise it). Applied as one choke point after
    all scorers so it covers ledger/pvp/pve/mining uniformly — including alts, since
    pvp/pve/mining attribute by the acting character, not the main.
    """
    from apps.pilots.models import PilotPreference
    from apps.sso.models import EveCharacter

    opted_out_users = PilotPreference.objects.filter(public_recognition=False).values_list(
        "user_id", flat=True
    )
    opted_out_cids = EveCharacter.objects.filter(user_id__in=opted_out_users).values_list(
        "character_id", flat=True
    )
    for cid in opted_out_cids:
        scores.pop(cid, None)


def scoreboard(year: int, month: int) -> dict:
    """Top-10 overall + top-5 per category for the given month (cached)."""
    from django.conf import settings

    cache_key = f"hof:{year:04d}-{month:02d}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    start, end = month_range(year, month)
    # 4.15: a completed month scores with its FROZEN weights so retuning the live weights
    # never silently reshuffles past boards; the current month tracks the live weights.
    weights = weights_for_month(year, month)

    scores: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    name_hints: dict[int, str] = {}
    _ledger_points(start, end, _user_main_char(), scores)
    _pvp_points(start, end, settings.FORCA_HOME_CORP_ID, weights, scores)
    _pve_points(start, end, weights, scores)
    _mining_points(start, end, weights, scores, name_hints)
    _drop_opted_out(scores)

    totals = {cid: sum(cats.values()) for cid, cats in scores.items()}
    names = _resolve_names(set(scores.keys()), name_hints)

    def row(cid: int, points: int) -> dict:
        return {"character_id": cid, "name": names.get(cid, f"#{cid}"), "points": points}

    overall = [
        {**row(cid, totals[cid]),
         "breakdown": sorted(
             ({"key": k, "label": CATEGORY_LABELS.get(k, k), "points": p}
              for k, p in scores[cid].items() if p),
             key=lambda d: -d["points"],
         )}
        for cid in sorted(totals, key=lambda c: -totals[c])[:OVERALL_TOP]
        if totals[cid] > 0
    ]

    # Every category is shown (even with no activity yet) so pilots can see what
    # counts and the weight applied.
    categories = []
    for key, label in CATEGORIES:
        ranked = sorted(
            ((cid, cats.get(key, 0)) for cid, cats in scores.items() if cats.get(key, 0) > 0),
            key=lambda t: -t[1],
        )[:CATEGORY_TOP]
        categories.append({
            "key": key, "label": label, "how": category_how(key, weights),
            "rows": [row(cid, pts) for cid, pts in ranked],
        })

    result = {
        "year": year, "month": month, "label": f"{start:%B %Y}",
        "overall": overall, "categories": categories,
        "scored": bool(overall),
        "scoring_enabled": weights.enabled,
    }
    cache.set(cache_key, result, _CACHE_TTL)
    return result


def invalidate_cache() -> None:
    """Drop cached scoreboards (call when weights change)."""
    cache.delete_many([f"hof:{m['key']}" for m in available_months()])
