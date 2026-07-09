"""Statistics, ESI-adoption metrics and per-pilot performance (read-only, cached).

Heavy aggregates are computed with a handful of grouped queries (never per-row
scans) and cached; the dashboard and admin stats pages read these rather than the
raw ledger.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal

from django.core.cache import cache
from django.db.models import Count, Sum
from django.utils import timezone

from .models import (
    RaffleIneligibleActivity,
    RaffleManualGrant,
    RaffleParticipantSummary,
    RaffleTicketLedgerEntry,
)

_log = logging.getLogger("forca.raffle")

_STATS_TTL = 300      # frozen contests — stats can't change
_LIVE_TTL = 45        # accruing contests — short TTL keeps the dashboard cheap but fresh
_ADOPTION_TTL = 600


# --------------------------------------------------------------------------- #
#  ESI-adoption metrics — the "grow the app" scoreboard
# --------------------------------------------------------------------------- #
def adoption_metrics(contest=None, *, use_cache: bool = True) -> dict:
    """Corp-wide enrolment funnel + (optionally) contest-specific ineligible counts."""
    key = f"raffle:adoption:{contest.pk if contest else 'global'}"
    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            return cached
    # use_cache=False means "recompute and REFRESH" (the warmer path) — it must
    # still write the cache below, not skip it.

    from django.conf import settings

    from apps.corporation.models import CorpMember, EveCorporation
    from apps.sso.models import AuthToken, EveCharacter

    # The DENOMINATOR must be the FULL corporation roster — every corp pilot,
    # whether or not they use the app — from ESI member-tracking (CorpMember).
    # Counting against EveCharacter would be circular: an EveCharacter row only
    # exists once a pilot has logged in (SSO), so ~everyone the app knows is
    # already "enrolled". The funnel is character-based to match the roster:
    #   total corp pilots  ⊇  enrolled (roster chars linked to an app account)
    #                      ⊇  with a currently-valid ESI token.
    home = int(getattr(settings, "FORCA_HOME_CORP_ID", 0) or 0)
    roster = CorpMember.objects.filter(corporation_id=home) if home else CorpMember.objects.all()
    roster_ids = set(roster.values_list("character_id", flat=True))

    if roster_ids:
        total_corp_pilots = len(roster_ids)
        enrolled_ids = set(
            EveCharacter.objects.filter(character_id__in=roster_ids, user__isnull=False)
            .values_list("character_id", flat=True)
        )
    else:
        # Member-tracking not synced (no Director corp_roster scope / disabled):
        # fall back to the corp's public headcount + app-known corp characters.
        corp = (EveCorporation.objects.filter(corporation_id=home).first() if home
                else EveCorporation.objects.filter(is_home_corp=True).first())
        enrolled_ids = set(
            EveCharacter.objects.filter(is_corp_member=True, user__isnull=False)
            .values_list("character_id", flat=True)
        )
        total_corp_pilots = (corp.member_count if corp and corp.member_count else 0) or len(enrolled_ids)

    enrolled = len(enrolled_ids)
    total_corp_pilots = max(total_corp_pilots, enrolled)  # enrolled can't exceed the roster

    with_valid_token = (
        AuthToken.objects.filter(character_id__in=enrolled_ids, revoked_at__isnull=True)
        .order_by().values("character_id").distinct().count()
        if enrolled_ids else 0
    )
    # Enrolled but no live token = expired/revoked.
    expired_or_revoked = max(0, enrolled - with_valid_token)
    # Corp pilots who never registered in the app (the outreach headroom).
    unenrolled = max(0, total_corp_pilots - enrolled)

    unenrolled_with_activity = 0
    if contest is not None:
        unenrolled_with_activity = (
            RaffleIneligibleActivity.objects.filter(contest=contest)
            .order_by().values("character_id").distinct().count()
        )

    conversion = round(100 * enrolled / total_corp_pilots, 1) if total_corp_pilots else 0.0
    token_rate = round(100 * with_valid_token / total_corp_pilots, 1) if total_corp_pilots else 0.0

    data = {
        "active_pilots": total_corp_pilots,   # total corp roster (all pilots)
        "enrolled": enrolled,
        "with_valid_token": with_valid_token,
        "expired_or_revoked": expired_or_revoked,
        "unenrolled": unenrolled,
        "unenrolled_with_activity": unenrolled_with_activity,
        "conversion_rate": conversion,
        "token_rate": token_rate,
        "roster_synced": bool(roster_ids),
        "as_of": timezone.now().isoformat(),
    }
    cache.set(key, data, _ADOPTION_TTL)
    return data


# --------------------------------------------------------------------------- #
#  Contest statistics
# --------------------------------------------------------------------------- #
def contest_statistics(contest, *, use_cache: bool = True) -> dict:
    key = f"raffle:stats:{contest.pk}"
    ttl = _LIVE_TTL if contest.is_accruing else _STATS_TTL
    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            return cached

    approved = RaffleTicketLedgerEntry.objects.filter(
        contest=contest, status=RaffleTicketLedgerEntry.Status.APPROVED
    )
    summaries = RaffleParticipantSummary.objects.filter(contest=contest)

    total_tickets = approved.aggregate(n=Sum("amount"))["n"] or 0
    participants = summaries.count()
    eligible_participants = summaries.filter(eligible=True).count()

    by_source = {
        r["source_key"]: r["n"]
        for r in approved.values("source_key").annotate(n=Sum("amount")).order_by("-n")
    }

    # PVP breakdown from summaries (already rolled up).
    pvp = summaries.aggregate(
        kills=Sum("pvp_kills"), participation=Sum("pvp_participation"),
        final_blows=Sum("pvp_final_blows"), solo=Sum("pvp_solo"),
    )

    # Tickets by day (accrual curve) — keyed by when the activity happened.
    by_day = defaultdict(int)
    for e in approved.values_list("occurred_at", "amount"):
        by_day[e[0].date().isoformat()] += e[1]
    tickets_by_day = [{"day": d, "tickets": n} for d, n in sorted(by_day.items())]

    manual = RaffleManualGrant.objects.filter(contest=contest)
    manual_total = manual.aggregate(n=Sum("amount"))["n"] or 0
    manual_by_category = list(
        manual.values("category").annotate(n=Sum("amount"), c=Count("id")).order_by("-n")
    )

    ineligible = RaffleIneligibleActivity.objects.filter(contest=contest)
    ineligible_events = ineligible.count()
    ineligible_by_reason = {
        r["reason"]: r["c"]
        for r in ineligible.order_by().values("reason").annotate(c=Count("id"))
    }

    kill_value = Decimal("0")
    for md in approved.filter(source_key="pvp").values_list("metadata", flat=True):
        try:
            kill_value += Decimal(str((md or {}).get("value", 0)))
        except Exception:  # noqa: BLE001 — a malformed metadata value must not break the page
            _log.debug("skipping unparseable raffle kill value in metadata", exc_info=True)

    top_pilots = list(
        summaries.filter(eligible=True).order_by("-total_tickets")
        .values("character_name", "total_tickets", "pvp_kills", "user_id")[:10]
    )

    prize_value = contest.prizes.aggregate(v=Sum("estimated_value"))["v"] or Decimal("0")
    tickets_per_prize_value = (
        float(total_tickets) / float(prize_value) if prize_value else 0.0
    )

    data = {
        "total_tickets": total_tickets,
        "participants": participants,
        "eligible_participants": eligible_participants,
        "by_source": by_source,
        "pvp": {k: (v or 0) for k, v in pvp.items()},
        "tickets_by_day": tickets_by_day,
        "manual_total": manual_total,
        "manual_by_category": manual_by_category,
        "ineligible_events": ineligible_events,
        "ineligible_by_reason": ineligible_by_reason,
        "kill_value": str(kill_value),
        "top_pilots": top_pilots,
        "prize_value": str(prize_value),
        "tickets_per_prize_value": round(tickets_per_prize_value, 2),
        "adoption": adoption_metrics(contest, use_cache=use_cache),
        "as_of": timezone.now().isoformat(),
    }
    cache.set(key, data, ttl)
    return data


def recommendations(contest) -> list[str]:
    """Simple post-contest insights for leadership."""
    stats = contest_statistics(contest)
    out = []
    by_source = stats["by_source"]
    if by_source:
        top = max(by_source.items(), key=lambda kv: kv[1])
        out.append(f"“{top[0]}” drove the most tickets ({top[1]:,}). Consider featuring it again.")
    adoption = stats["adoption"]
    if adoption["unenrolled_with_activity"]:
        out.append(
            f"{adoption['unenrolled_with_activity']} active pilots earned no tickets because they "
            "aren't enrolled — a great outreach list to grow app adoption."
        )
    if adoption["expired_or_revoked"]:
        out.append(
            f"{adoption['expired_or_revoked']} enrolled pilots have an expired/revoked ESI token — "
            "nudge them to reconnect before the next contest."
        )
    if stats["eligible_participants"] < max(1, stats["participants"] // 2):
        out.append("Under half of participants were eligible — the enrolment CTA is working; keep pushing it.")
    if not out:
        out.append("Healthy contest. Repeat the format and try a booster weekend to lift engagement.")
    return out


# --------------------------------------------------------------------------- #
#  Per-pilot performance (private page)
# --------------------------------------------------------------------------- #
def pilot_performance(contest, user) -> dict:
    """Everything the private performance page needs for one account."""
    from . import eligibility as elig

    e = elig.for_user(contest, user)
    summary = RaffleParticipantSummary.objects.filter(contest=contest, user=user).first()
    entries = list(
        RaffleTicketLedgerEntry.objects.filter(contest=contest, user=user)
        .order_by("-created_at")[:100]
    )
    approved = [x for x in entries if x.status == RaffleTicketLedgerEntry.Status.APPROVED]

    by_day = defaultdict(int)
    for x in approved:
        by_day[x.created_at.date().isoformat()] += x.amount
    activity_by_day = [{"day": d, "tickets": n} for d, n in sorted(by_day.items())]

    total_eligible = (
        RaffleParticipantSummary.objects.filter(contest=contest, eligible=True)
        .aggregate(n=Sum("total_tickets"))["n"] or 0
    )
    my_tickets = summary.total_tickets if summary else 0
    odds = round(100 * my_tickets / total_eligible, 2) if total_eligible else 0.0

    return {
        "eligibility": e,
        "summary": summary,
        "entries": entries,
        "activity_by_day": activity_by_day,
        "odds": odds,
        "my_tickets": my_tickets,
        "total_eligible_tickets": total_eligible,
    }
