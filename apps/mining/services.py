"""Mining participation valuation, tax, and payout computation."""
from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Sum
from django.utils import timezone

from apps.market.pricing import price_for

from .models import MiningLedgerEntry, MiningMilestone, MiningPayoutLine, MiningTaxConfig


def _q(value) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def active_tax_rate() -> Decimal:
    cfg = MiningTaxConfig.objects.filter(is_active=True).order_by("-updated_at").first()
    return cfg.rate if cfg else Decimal("0.10")


def participation(start: dt.date, end: dt.date) -> list[dict]:
    """Per-pilot mining over [start, end]: quantity and Jita-valued ISK, valued at
    each ore's Jita sell price. Sorted by value, descending."""
    rows = (
        MiningLedgerEntry.objects.filter(day__gte=start, day__lte=end)
        .values("character_id", "character_name", "type_id")
        .annotate(qty=Sum("quantity"))
    )
    # Price each ore type once.
    type_ids = {r["type_id"] for r in rows}
    prices = {tid: price_for(tid) for tid in type_ids}

    by_pilot: dict[int, dict] = {}
    for r in rows:
        cid = r["character_id"]
        p = by_pilot.setdefault(cid, {"character_id": cid, "name": r["character_name"] or f"#{cid}",
                                      "quantity": 0, "value": Decimal("0")})
        p["quantity"] += r["qty"]
        p["value"] += prices.get(r["type_id"], Decimal("0")) * r["qty"]
        if r["character_name"]:
            p["name"] = r["character_name"]
    out = list(by_pilot.values())
    for p in out:
        p["value"] = _q(p["value"])
    out.sort(key=lambda p: p["value"], reverse=True)
    return out


def my_mining_summary(character_ids, start: dt.date, end: dt.date) -> dict:
    """One pilot's own mining over [start, end], across all their characters.

    Per-ore quantity, m³ (quantity × the ore's unit volume) and Jita-sell ISK value,
    plus totals. Empty when the pilot has no recorded mining. Only refinery-observer
    mining is captured, so belt/nomad mining is necessarily absent (caveated in the UI).
    """
    cids = list(character_ids)
    empty = {"rows": [], "total_quantity": 0, "total_m3": Decimal("0"), "total_value": Decimal("0")}
    if not cids:
        return empty
    agg = (
        MiningLedgerEntry.objects.filter(character_id__in=cids, day__gte=start, day__lte=end)
        .values("type_id")
        .annotate(qty=Sum("quantity"))
    )
    type_ids = [r["type_id"] for r in agg]
    if not type_ids:
        return empty
    prices = {tid: price_for(tid) for tid in type_ids}
    from apps.sde.models import SdeType

    meta = {
        t["type_id"]: (t["name"], Decimal(str(t["volume"] or 0)))
        for t in SdeType.objects.filter(type_id__in=type_ids).values("type_id", "name", "volume")
    }
    rows, total_qty, total_m3, total_value = [], 0, Decimal("0"), Decimal("0")
    for r in agg:
        tid, qty = r["type_id"], r["qty"] or 0
        name, vol = meta.get(tid, (f"Type {tid}", Decimal("0")))
        m3 = vol * qty
        value = _q(prices.get(tid, Decimal("0")) * qty)
        rows.append({"type_id": tid, "name": name, "quantity": qty, "m3": m3, "value": value})
        total_qty += qty
        total_m3 += m3
        total_value += value
    rows.sort(key=lambda x: x["value"], reverse=True)
    return {
        "rows": rows, "total_quantity": total_qty,
        "total_m3": total_m3.quantize(Decimal("0.01")), "total_value": _q(total_value),
    }


def my_payout_lines(user, character_ids) -> dict:
    """A pilot's mining-payout lines (owed vs paid), newest window first.

    Keyed on the pilot's characters (payout lines are keyed by character, even when the
    account link was absent at payout time), plus a direct user match — so a pilot always
    sees every line that is theirs and nobody else's.
    """
    from django.db.models import Q

    qs = MiningPayoutLine.objects.filter(Q(user=user) | Q(character_id__in=list(character_ids)))
    # Totals come from a DB aggregate over *all* the pilot's lines, so they stay accurate
    # even though the displayed list is bounded.
    agg = qs.aggregate(
        owed=Sum("net", filter=Q(paid=False)),
        paid=Sum("net", filter=Q(paid=True)),
    )
    lines = list(
        qs.select_related("payout").order_by("-payout__period_end", "-payout_id")[:200]
    )
    return {"lines": lines, "owed": _q(agg["owed"] or 0), "paid": _q(agg["paid"] or 0)}


def my_mining_tickets(user) -> int:
    """Best-effort: the pilot's approved mining raffle tickets across active contests.

    Returns 0 if the raffle subsystem has no active mining contest (or on any error) —
    a raffle problem must never break the mining page.
    """
    try:
        from django.db.models import Sum as _Sum

        from apps.raffle.models import RaffleContest, RaffleTicketLedgerEntry

        total = RaffleTicketLedgerEntry.objects.filter(
            user=user, source_key="mining",
            status=RaffleTicketLedgerEntry.Status.APPROVED,
            contest__status=RaffleContest.Status.ACTIVE,
        ).aggregate(s=_Sum("amount"))["s"]
        return int(total or 0)
    except Exception:  # noqa: BLE001 — the mining page must survive a raffle hiccup
        return 0


def build_payout(payout) -> int:
    """(Re)compute a payout's per-pilot lines from participation in its window.

    Splits ``pool_isk`` by the chosen method, then withholds the mining tax so each
    line's net is what the pilot is paid and the tax total is what the corp keeps.
    """
    from apps.sso.models import EveCharacter

    parts = participation(payout.period_start, payout.period_end)
    payout.lines.all().delete()
    if not parts:
        payout.total_value = Decimal("0")
        payout.save(update_fields=["total_value", "updated_at"])
        return 0

    total_value = sum((p["value"] for p in parts), start=Decimal("0"))
    total_qty = sum(p["quantity"] for p in parts)
    pool = Decimal(payout.pool_isk)
    rate = Decimal(payout.tax_rate)
    n = len(parts)
    user_by_char = dict(
        EveCharacter.objects.filter(character_id__in=[p["character_id"] for p in parts])
        .values_list("character_id", "user_id")
    )

    created = 0
    for p in parts:
        if payout.method == payout.Method.EQUAL:
            share = Decimal(1) / n
        elif payout.method == payout.Method.BY_VOLUME:
            share = (Decimal(p["quantity"]) / total_qty) if total_qty else Decimal("0")
        else:  # BY_VALUE
            share = (p["value"] / total_value) if total_value else Decimal("0")
        gross = _q(pool * share)
        tax = _q(gross * rate)
        MiningPayoutLine.objects.create(
            payout=payout, character_id=p["character_id"], character_name=p["name"],
            user_id=user_by_char.get(p["character_id"]),
            value_mined=p["value"], share_pct=_q(share * 100),
            gross=gross, tax=tax, net=_q(gross - tax),
        )
        created += 1

    payout.total_value = _q(total_value)
    payout.save(update_fields=["total_value", "updated_at"])
    return created


# --- MIN-4 (3.10): mining participation milestones ---------------------------
MINING_MILESTONES = [1_000_000, 10_000_000, 50_000_000, 100_000_000, 500_000_000, 1_000_000_000]
_MILESTONE_POINTS = {
    1_000_000: 5, 10_000_000: 10, 50_000_000: 20,
    100_000_000: 30, 500_000_000: 50, 1_000_000_000: 100,
}
_MILESTONE_BASELINED_KEY = "mining.milestones_baselined"


def cumulative_m3(character_ids) -> int:
    """All-time m³ mined across a pilot's characters (refinery-observer records only)."""
    cids = list(character_ids)
    if not cids:
        return 0
    from apps.sde.models import SdeType

    rows = list(
        MiningLedgerEntry.objects.filter(character_id__in=cids)
        .values("type_id").annotate(qty=Sum("quantity"))
    )
    vols = dict(
        SdeType.objects.filter(type_id__in=[r["type_id"] for r in rows])
        .values_list("type_id", "volume")
    )
    total = Decimal("0")
    for r in rows:
        total += Decimal(str(vols.get(r["type_id"]) or 0)) * (r["qty"] or 0)
    return int(total)


def mining_milestones(character_ids) -> dict:
    """A pilot's cumulative m³, reached milestones, next target, and recent-month activity —
    honest progression for the industrial backbone (display, no surveillance)."""
    cum = cumulative_m3(character_ids)
    nxt = next((m for m in MINING_MILESTONES if m > cum), None)
    cids = list(character_ids)
    months = 0
    if cids:
        since = timezone.now().date() - dt.timedelta(days=183)
        days = MiningLedgerEntry.objects.filter(
            character_id__in=cids, day__gte=since
        ).values_list("day", flat=True)
        months = len({(d.year, d.month) for d in days})
    return {
        "cumulative_m3": cum,
        "reached": [m for m in MINING_MILESTONES if cum >= m],
        "next_threshold": nxt,
        "remaining": (nxt - cum) if nxt else 0,
        "months_active_6mo": months,
    }


def scan_mining_milestones() -> dict:
    """Award recognition for newly-crossed cumulative-m³ milestones (MIN-4 / 3.10).

    Future-only: the FIRST scan snapshots every pilot's already-reached milestones as an
    un-credited baseline; only crossings AFTER the baseline earn a ContributionEvent.
    Never moves ISK.
    """
    from collections import defaultdict

    from django.contrib.auth import get_user_model

    from apps.admin_audit.models import AppSetting
    from apps.pilots.models import ContributionEvent
    from apps.pilots.services import record_contribution
    from apps.sso.models import EveCharacter

    baselined = bool(AppSetting.get(_MILESTONE_BASELINED_KEY))
    now = timezone.now()

    mined = MiningLedgerEntry.objects.values_list("character_id", flat=True).distinct()
    user_cids: dict[int, list[int]] = defaultdict(list)
    for c in EveCharacter.objects.filter(
        character_id__in=list(mined), user__isnull=False
    ).values("user_id", "character_id"):
        user_cids[c["user_id"]].append(c["character_id"])

    users = {u.id: u for u in get_user_model().objects.filter(id__in=list(user_cids))}
    awarded = 0
    for user_id, cids in user_cids.items():
        cum = cumulative_m3(cids)
        for threshold in MINING_MILESTONES:
            if cum < threshold:
                break  # thresholds ascending — nothing higher is reached
            # get_or_create (not exists()+create) so two overlapping scans can't race the
            # unique constraint into an IntegrityError; the loser just skips.
            _m, created = MiningMilestone.objects.get_or_create(
                user_id=user_id, threshold_m3=threshold,
                defaults={"reached_at": now, "credited": baselined},
            )
            if not created:
                continue
            if baselined and user_id in users:  # not the baseline run → a real new crossing
                record_contribution(
                    users[user_id], kind=ContributionEvent.Kind.MINING, magnitude=1,
                    unit="milestones", points=_MILESTONE_POINTS.get(threshold, 5),
                    description=f"Mined {threshold // 1_000_000}M m³",
                    ref_type="mining_milestone", ref_id=f"{user_id}:{threshold}",
                    occurred_at=now,
                )
                awarded += 1

    if not baselined:
        AppSetting.objects.update_or_create(
            key=_MILESTONE_BASELINED_KEY, defaults={"value": True}
        )
    return {"awarded": awarded, "baselined_now": not baselined}
