"""Rank-progression rewards: future-only baseline, event generation, the
income-aware suggestion engine, and the reward-event lifecycle.

Design guarantees (see docs/killboard/combat-ranks.md):

* **The system never moves ISK.** Reaching a reward-enabled rank only creates a
  *pending* :class:`RankRewardEvent`; a leader approves and marks it paid by hand.
* **Rewards are future-only.** When rewards are enabled, every pilot's current
  highest rank is snapshotted as a :class:`PilotRankBaseline`; a pilot only earns a
  reward for a reward-enabled rung strictly **above** that baseline. A pilot who
  enrolls/joins later is baselined at their current rank on first sight, so they can
  never claim rungs they already held.
* **Enrolled pilots only.** Events are generated solely for pilots with a linked
  account and a healthy ESI token (DB-only check, request-safe — mirrors the raffle
  eligibility rule); statistically-qualifying but unenrolled pilots create nothing.
* **Idempotent.** ``(character_id, rank_min_kills)`` is unique, so a rung can never be
  awarded twice.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.core.exceptions import PermissionDenied
from django.db.models import Sum
from django.utils import timezone

from . import ranks
from .models import (
    CombatRankTitle,
    MonthlyPilotKillStat,
    PilotRankBaseline,
    RankMetric,
    RankRewardEvent,
    RankRewardSettings,
    RewardType,
)

_REFRESH_FAIL_LIMIT = 5  # matches apps.raffle.eligibility — a stalled token is "unhealthy"

STRATEGY_MULT = {"conservative": Decimal("0.5"), "standard": Decimal("1"), "aggressive": Decimal("1.75")}
PLEX_TYPE_ID = 44992


# --------------------------------------------------------------------------- #
#  Enrolment / eligibility (DB-only, request-safe)
# --------------------------------------------------------------------------- #
def enrolled_eligible_character_ids() -> set[int]:
    """Home-corp characters that are enrolled (linked account) AND hold a healthy token."""
    from apps.sso.models import EveCharacter

    out: set[int] = set()
    chars = (
        EveCharacter.objects.filter(is_corp_member=True, user__isnull=False)
        .prefetch_related("tokens")
    )
    for ch in chars:
        for t in ch.tokens.all():
            if t.revoked_at is None and t._refresh_token and (t.refresh_fail_count or 0) < _REFRESH_FAIL_LIMIT:
                out.add(ch.character_id)
                break
    return out


def account_reward_eligible(character_ids) -> bool:
    """Cheap per-account eligibility for the dashboard hint (any healthy token)."""
    from apps.sso.models import AuthToken

    return (
        AuthToken.objects.filter(
            character_id__in=list(character_ids),
            revoked_at__isnull=True,
            refresh_fail_count__lt=_REFRESH_FAIL_LIMIT,
        )
        .exclude(_refresh_token="")
        .exists()
    )


# --------------------------------------------------------------------------- #
#  Kill counts
# --------------------------------------------------------------------------- #
def all_time_kills_map() -> dict[int, int]:
    """character_id → all-time PvP kills, from the nightly CombatMetric rollup
    (fast) with a live fallback before the first rebuild."""
    from .models import CombatMetric

    rows = CombatMetric.objects.filter(
        entity_type=CombatMetric.EntityType.CHARACTER, window="all"
    ).values_list("entity_id", "kills")
    m = {cid: k for cid, k in rows}
    if m:
        return m
    from .leaderboards import _merge_pilots, window_for

    return {cid: (p.get("kills", 0) or 0) for cid, p in _merge_pilots(window_for("all")).items()}


def _recent_kill_rate() -> dict[int, int]:
    """Kills per pilot over roughly the last 90 days (last 3 calendar months), from
    the monthly aggregate — used to project future rank-ups."""
    from .aggregation import _EVE_TZ

    now = timezone.now().astimezone(_EVE_TZ)
    periods = []
    y, m = now.year, now.month
    for _ in range(3):
        periods.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    from django.db.models import Q

    filt = Q()
    for (yy, mm) in periods:
        filt |= Q(year=yy, month=mm)
    rows = (
        MonthlyPilotKillStat.objects.filter(filt)
        .order_by()
        .values("character_id")
        .annotate(k=Sum("kills"))
    )
    return {r["character_id"]: (r["k"] or 0) for r in rows}


# --------------------------------------------------------------------------- #
#  Baseline (future-only guarantee)
# --------------------------------------------------------------------------- #
def _rank_id_for(ladder: list[dict], tier: int):
    return ladder[tier]["id"] if 0 <= tier < len(ladder) else None


def establish_baseline(actor=None) -> int:
    """Snapshot every pilot's current highest rank and enable rewards.

    Called when leadership turns rewards on (or re-baselines after big ladder edits).
    Safe to re-run — it re-snapshots the baseline to *now*, which can only ever raise
    a pilot's baseline, never create retroactive liability.
    """
    from apps.sso.models import EveCharacter

    ladder = ranks.active_ladder()
    kills_map = all_time_kills_map()
    members = dict(
        EveCharacter.objects.filter(is_corp_member=True).values_list("character_id", "name")
    )
    ids = set(kills_map) | set(members)
    now = timezone.now()
    for cid in ids:
        kills = kills_map.get(cid, 0)
        cur = ranks.combat_rank(kills, ladder)
        PilotRankBaseline.objects.update_or_create(
            character_id=cid,
            defaults={
                "character_name": members.get(cid, ""),
                "baseline_rank_id": _rank_id_for(ladder, cur["tier"]),
                "baseline_min_kills": cur["min_kills"],
                "baseline_kills": kills,
                "established_at": now,
            },
        )
    s = RankRewardSettings.load()
    s.rewards_enabled = True
    s.baseline_established_at = now
    s.updated_by = actor if getattr(actor, "pk", None) else None
    s.save(update_fields=["rewards_enabled", "baseline_established_at", "updated_by", "updated_at"])
    return len(ids)


def disable_rewards(actor=None) -> None:
    """Turn the reward engine off (baseline is kept, so re-enabling doesn't backfill)."""
    s = RankRewardSettings.load()
    s.rewards_enabled = False
    s.updated_by = actor if getattr(actor, "pk", None) else None
    s.save(update_fields=["rewards_enabled", "updated_by", "updated_at"])


# --------------------------------------------------------------------------- #
#  Event generation
# --------------------------------------------------------------------------- #
def scan_and_award(actor=None) -> int:
    """Create pending reward events for enrolled pilots who crossed a reward-enabled
    rank above their baseline. Idempotent; returns the number of new events."""
    s = RankRewardSettings.load()
    if not s.rewards_enabled or not s.baseline_established_at:
        return 0

    reward_ranks = list(
        CombatRankTitle.objects.filter(
            is_active=True, metric=RankMetric.KILLS, grants_reward=True
        ).exclude(reward_type=RewardType.NONE).order_by("min_kills")
    )
    if not reward_ranks:
        return 0

    ladder = ranks.active_ladder()
    ladder_by_min = {e["min_kills"]: e for e in ladder}
    eligible_ids = enrolled_eligible_character_ids()
    if not eligible_ids:
        return 0
    kills_map = all_time_kills_map()

    from apps.sso.models import EveCharacter

    meta = {
        c["character_id"]: (c["name"], c["user_id"])
        for c in EveCharacter.objects.filter(character_id__in=eligible_ids).values(
            "character_id", "name", "user_id"
        )
    }
    baselines = {
        b.character_id: b
        for b in PilotRankBaseline.objects.filter(character_id__in=eligible_ids)
    }
    now = timezone.now()
    created = 0
    for cid in eligible_ids:
        kills = kills_map.get(cid, 0)
        name, user_id = meta.get(cid, ("", None))
        base = baselines.get(cid)
        if base is None:
            # Enrolled/joined after the baseline was taken: baseline them at their
            # CURRENT rank now and award nothing retroactively this cycle.
            cur = ranks.combat_rank(kills, ladder)
            PilotRankBaseline.objects.get_or_create(
                character_id=cid,
                defaults={
                    "character_name": name, "baseline_rank_id": _rank_id_for(ladder, cur["tier"]),
                    "baseline_min_kills": cur["min_kills"], "baseline_kills": kills,
                    "established_at": now,
                },
            )
            continue

        # Anchor on the pilot's actual kill count at baseline, not just the rung
        # threshold they held — otherwise inserting a NEW reward rank between the
        # baseline rung and their baseline kills (without re-baselining) would grant
        # a retroactive reward for a threshold they'd already passed. baseline_kills
        # is >= baseline_min_kills, so max() is belt-and-braces against odd data.
        base_floor = max(base.baseline_min_kills, base.baseline_kills)
        for rank in reward_ranks:
            if base_floor < rank.min_kills <= kills:
                entry = ladder_by_min.get(rank.min_kills)
                prev_name = ""
                if entry and entry["tier"] > 0:
                    prev_name = ladder[entry["tier"] - 1]["name"]
                _, was_created = RankRewardEvent.objects.get_or_create(
                    character_id=cid,
                    rank_min_kills=rank.min_kills,
                    defaults={
                        "character_name": name, "user_id": user_id, "rank_id": rank.id,
                        "rank_name": rank.name, "previous_rank_name": prev_name,
                        "kills_at_award": kills, "achieved_at": now,
                        "reward_type": rank.reward_type, "reward_amount": rank.reward_amount,
                        "reward_item_type_id": rank.reward_item_type_id,
                        "status": RankRewardEvent.Status.PENDING,
                    },
                )
                if was_created:
                    created += 1
    return created


# --------------------------------------------------------------------------- #
#  Event lifecycle (state transitions — the console writes the audit rows)
# --------------------------------------------------------------------------- #
class InvalidTransition(Exception):
    pass


def _deny_self_action(event: RankRewardEvent, actor) -> None:
    """Separation of duties: a pilot must never approve or pay their OWN reward.
    Mirrors SRP (``services.decide``/``mark_paid``) and the raffle grant desk. A
    superuser break-glass is allowed, matching those flows."""
    actor_id = getattr(actor, "id", None)
    if (event.user_id and actor_id and event.user_id == actor_id
            and not getattr(actor, "is_superuser", False)):
        raise PermissionDenied("You can't approve or pay your own combat-rank reward.")


def approve(event: RankRewardEvent, actor) -> RankRewardEvent:
    _deny_self_action(event, actor)
    if event.status != RankRewardEvent.Status.PENDING:
        raise InvalidTransition("Only pending rewards can be approved.")
    event.status = RankRewardEvent.Status.APPROVED
    event.approved_by = actor if getattr(actor, "pk", None) else None
    event.approved_at = timezone.now()
    event.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    return event


def mark_paid(event: RankRewardEvent, actor, *, reference: str = "") -> RankRewardEvent:
    _deny_self_action(event, actor)
    if event.status not in (RankRewardEvent.Status.APPROVED, RankRewardEvent.Status.PENDING):
        raise InvalidTransition("Only pending/approved rewards can be marked paid.")
    event.status = RankRewardEvent.Status.PAID
    event.paid_by = actor if getattr(actor, "pk", None) else None
    event.paid_at = timezone.now()
    if reference:
        event.payment_reference = reference
    event.save(update_fields=["status", "paid_by", "paid_at", "payment_reference", "updated_at"])
    return event


def reject(event: RankRewardEvent, actor, *, reason: str = "") -> RankRewardEvent:
    if event.status in (RankRewardEvent.Status.PAID, RankRewardEvent.Status.CANCELLED):
        raise InvalidTransition("A paid/cancelled reward can't be rejected.")
    event.status = RankRewardEvent.Status.REJECTED
    if reason:
        event.notes = (event.notes + f"\nRejected: {reason}").strip()
    event.save(update_fields=["status", "notes", "updated_at"])
    return event


def cancel(event: RankRewardEvent, actor, *, reason: str = "") -> RankRewardEvent:
    if event.status == RankRewardEvent.Status.PAID:
        raise InvalidTransition("A paid reward can't be cancelled.")
    event.status = RankRewardEvent.Status.CANCELLED
    if reason:
        event.notes = (event.notes + f"\nCancelled: {reason}").strip()
    event.save(update_fields=["status", "notes", "updated_at"])
    return event


# --------------------------------------------------------------------------- #
#  ISK valuation helpers
# --------------------------------------------------------------------------- #
def plex_isk_rate(settings=None) -> Decimal:
    """ISK per PLEX for liability estimates: the admin override, else live market."""
    s = settings or RankRewardSettings.load()
    if s.plex_isk_rate and s.plex_isk_rate > 0:
        return s.plex_isk_rate
    try:
        from apps.market.pricing import price_for

        p = price_for(PLEX_TYPE_ID)
        return Decimal(str(p)) if p else Decimal("0")
    except Exception:  # noqa: BLE001 - market not synced yet → no ISK estimate
        return Decimal("0")


def reward_isk_value(reward_type: str, amount, *, rate: Decimal | None = None) -> Decimal:
    """ISK-equivalent of a reward for liability totals (item/manual aren't quantified)."""
    amount = Decimal(str(amount or 0))
    if reward_type == RewardType.ISK:
        return amount
    if reward_type == RewardType.PLEX:
        return amount * (rate if rate is not None else plex_isk_rate())
    return Decimal("0")


# --------------------------------------------------------------------------- #
#  Distribution snapshots + the suggestion engine
# --------------------------------------------------------------------------- #
def pilots_at_each_rank() -> dict[int, int]:
    """Count of pilots whose current highest rank == each rung (keyed by min_kills)."""
    ladder = ranks.active_ladder()
    kills_map = all_time_kills_map()
    counts: dict[int, int] = defaultdict(int)
    for kills in kills_map.values():
        counts[ranks.combat_rank(kills, ladder)["min_kills"]] += 1
    return dict(counts)


def project_rank_ups(days: int = 30) -> dict[int, float]:
    """Estimated pilots crossing INTO each rank within ``days``, from recent pace."""
    ladder = ranks.active_ladder()
    kills_map = all_time_kills_map()
    rate90 = _recent_kill_rate()
    per_rank: dict[int, float] = defaultdict(float)
    for cid, kills in kills_map.items():
        rate = (rate90.get(cid, 0) or 0) / 90.0
        projected = int(kills + rate * days)
        cur_tier = ranks.combat_rank(kills, ladder)["tier"]
        proj_tier = ranks.combat_rank(projected, ladder)["tier"]
        for t in range(cur_tier + 1, proj_tier + 1):
            per_rank[ladder[t]["min_kills"]] += 1.0
    return dict(per_rank)


def _prestige_weight(min_kills: int) -> Decimal:
    """A smooth, rarity-increasing weight (√ of the threshold, floored at 1)."""
    return Decimal(max(1.0, float(min_kills) ** 0.5)).quantize(Decimal("0.01"))


def _round_isk(x: Decimal) -> Decimal:
    """Round an ISK suggestion to a clean figure (nearest 1M, or 100k when small)."""
    if x <= 0:
        return Decimal("0")
    step = Decimal("1000000") if x >= Decimal("10000000") else Decimal("100000")
    return (x / step).quantize(Decimal("1")) * step


def reward_pool_isk(settings=None) -> tuple[Decimal, str]:
    """The monthly ISK reward pool + where it came from ('income' | 'budget' | 'unset')."""
    s = settings or RankRewardSettings.load()
    pool: Decimal | None = None
    source = "unset"
    income = monthly_income_estimate()
    if income is not None and s.max_income_pct and s.max_income_pct > 0:
        pool = income * s.max_income_pct / Decimal("100")
        source = "income"
    if pool is None and s.monthly_budget and s.monthly_budget > 0:
        pool = s.monthly_budget
        source = "budget"
    if pool is None:
        pool = Decimal("0")
    if s.monthly_cap and s.monthly_cap > 0 and pool > s.monthly_cap:
        pool = s.monthly_cap
    return pool, source


def monthly_income_estimate() -> Decimal | None:
    """Last-30-day gross corp income (a monthly proxy), or None if finance isn't synced."""
    from django.core.cache import cache

    key = "kb:ranks:monthly_income"
    cached = cache.get(key)
    if cached is not None:
        return cached if cached != "none" else None
    value: Decimal | None = None
    try:
        from apps.corporation.finance_analytics import finance_dashboard
        from apps.corporation.models import CorpWalletJournalEntry

        if CorpWalletJournalEntry.objects.exists():
            value = finance_dashboard("30d").get("income_total") or None
    except Exception:  # noqa: BLE001 - finance optional (needs the corp wallet scope)
        value = None
    cache.set(key, value if value is not None else "none", 600)
    return value


def reward_suggestions() -> dict:
    """Per-rank suggested reward amounts (conservative / standard / aggressive) plus the
    monthly liability each strategy implies, sized to the corp's income or budget.

    Method (transparent, documented in docs/killboard/combat-ranks.md): a monthly ISK
    pool is derived from income × max%% (or the configured budget), capped. Each rank
    gets a prestige weight (√ threshold) so rarer ranks pay more; expected monthly
    rank-ups come from recent pace. Per-award amounts are solved so the *standard*
    strategy's total expected monthly spend ≈ the pool, then scaled by strategy.
    """
    s = RankRewardSettings.load()
    ladder = ranks.active_ladder()
    pool, source = reward_pool_isk(s)
    expected = project_rank_ups(30)

    weights = {e["min_kills"]: _prestige_weight(e["min_kills"]) for e in ladder}
    # Denominator: weighted expected rank-ups. If we can't project (sparse data),
    # fall back to weight-only so amounts still increase sensibly with rarity.
    denom = sum(weights[mk] * Decimal(str(expected.get(mk, 0))) for mk in weights)
    weight_only = False
    if denom <= 0:
        denom = sum(weights.values())
        weight_only = True

    base = (pool / denom) if denom > 0 else Decimal("0")
    per_award_standard = {mk: _round_isk(base * weights[mk]) for mk in weights}

    strategies = {}
    for name, mult in STRATEGY_MULT.items():
        amounts = {mk: _round_isk(per_award_standard[mk] * mult) for mk in weights}
        # Liability = Σ per-award × expected monthly rank-ups (only where reward-enabled).
        reward_mins = {e["min_kills"] for e in ladder if e["grants_reward"]}
        liability = sum(
            amounts[mk] * Decimal(str(expected.get(mk, 0)))
            for mk in weights if mk in reward_mins
        )
        strategies[name] = {"amounts": amounts, "monthly_liability": liability}

    return {
        "pool": pool,
        "pool_source": source,
        "weight_only": weight_only,
        "expected_rank_ups": expected,
        "strategies": strategies,
        "monthly_income": monthly_income_estimate(),
    }


def estimated_monthly_liability(settings=None) -> Decimal:
    """Expected monthly ISK liability from the CURRENTLY-configured reward ranks."""
    s = settings or RankRewardSettings.load()
    rate = plex_isk_rate(s)
    expected = project_rank_ups(30)
    total = Decimal("0")
    for rank in CombatRankTitle.objects.filter(
        is_active=True, metric=RankMetric.KILLS, grants_reward=True
    ).exclude(reward_type=RewardType.NONE):
        isk = reward_isk_value(rank.reward_type, rank.reward_amount, rate=rate)
        total += isk * Decimal(str(expected.get(rank.min_kills, 0)))
    return total


def pending_liability() -> Decimal:
    """ISK-equivalent of all pending + approved (unpaid) reward events."""
    rate = plex_isk_rate()
    total = Decimal("0")
    for e in RankRewardEvent.objects.filter(
        status__in=[RankRewardEvent.Status.PENDING, RankRewardEvent.Status.APPROVED]
    ).values("reward_type", "reward_amount"):
        total += reward_isk_value(e["reward_type"], e["reward_amount"], rate=rate)
    return total


def rank_admin_rows() -> list[dict]:
    """Per-rank overview for the admin ladder page: pilots now + projected rank-ups."""
    ladder = ranks.active_ladder()
    at_rank = pilots_at_each_rank()
    p30 = project_rank_ups(30)
    p90 = project_rank_ups(90)
    p180 = project_rank_ups(180)
    rows = []
    for e in ladder:
        mk = e["min_kills"]
        rows.append({
            "min_kills": mk, "name": e["name"], "tier": e["tier"],
            "pilots_now": at_rank.get(mk, 0),
            "proj_30": round(p30.get(mk, 0)), "proj_90": round(p90.get(mk, 0)),
            "proj_180": round(p180.get(mk, 0)),
            "grants_reward": e["grants_reward"], "reward_type": e["reward_type"],
            "reward_amount": e["reward_amount"],
        })
    return rows


# --------------------------------------------------------------------------- #
#  Dashboard helper
# --------------------------------------------------------------------------- #
def reward_dashboard(character_ids) -> dict:
    """Reward context for the pilot dashboard rank card (cheap, per-account)."""
    s = RankRewardSettings.load()
    ids = list(character_ids)
    return {
        "enabled": s.rewards_enabled,
        "eligible": account_reward_eligible(ids) if s.rewards_enabled else False,
        "pending": (
            RankRewardEvent.objects.filter(
                character_id__in=ids, status=RankRewardEvent.Status.PENDING
            ).count() if s.rewards_enabled else 0
        ),
    }
