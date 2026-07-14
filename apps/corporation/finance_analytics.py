"""Corp finance analytics: windows, categories, series, KPIs and a forecast.

One read-only layer over the corp wallet journal we already sync. The journal is
small (a few thousand rows), so the window's entries are loaded once and every
metric is computed in Python — clearer than a dozen separate aggregates. Heavy
enough to cache: the default view is memoised and warmed by a beat task.
"""
from __future__ import annotations

import datetime as dt
import statistics
from decimal import Decimal

from django.utils import timezone
from django.utils.translation import gettext_lazy as _

# --- ref_type → human category ----------------------------------------------
# Buckets the raw ESI ref_types so income/expense read in plain language. The
# sign of the amount decides income vs expense; this is just the label.
#
# The labels are lazy on purpose: this dashboard is memoised under a locale-free
# cache key and warmed by a beat task, so an eager gettext() would freeze the
# filler's language into the shared blob for the whole TTL. Lazy proxies pickle
# and re-resolve per viewer, and they hash/compare equal, so using them as
# aggregation keys still works.
_REF_CATEGORY = {
    # Ratting / PvE income
    "bounty_prizes": _("Ratting"), "bounty_prize": _("Ratting"),
    "ess_escrow_transfer": _("Ratting"), "daily_goal_payouts": _("Ratting"),
    "agent_mission_reward": _("Missions"), "agent_mission_time_bonus_reward": _("Missions"),
    "project_discovery_reward": _("Ratting"), "corporate_reward_payout": _("Payouts"),
    "corporate_reward_tax": _("Taxes"),
    # Industry
    "industry_job_tax": _("Industry"), "reprocessing_tax": _("Industry"),
    "manufacturing": _("Industry"), "researching_technology": _("Industry"),
    "researching_time_productivity": _("Industry"),
    "researching_material_productivity": _("Industry"),
    "copying": _("Industry"), "reaction": _("Industry"),
    # Market
    "market_transaction": _("Market"), "transaction_tax": _("Market"),
    "brokers_fee": _("Market"), "market_escrow": _("Market"), "market_provider_tax": _("Market"),
    "market_fine_paid": _("Market"),
    # Contracts
    "contract_price_payment_corp": _("Contracts"), "contract_brokers_fee": _("Contracts"),
    "contract_reward": _("Contracts"), "contract_reward_refund": _("Contracts"),
    "contract_collateral": _("Contracts"), "contract_price": _("Contracts"),
    "contract_deposit": _("Contracts"),
    # Transfers / donations
    "player_donation": _("Donations"), "corporation_account_withdrawal": _("Transfers"),
    "corp_withdrawal": _("Transfers"), "player_trading": _("Transfers"),
    "donation": _("Donations"),
    # Structures / sov / fees
    "office_rental_fee": _("Structures"), "structure_gate_jump": _("Structures"),
    "sovereignty_bill": _("Structures"), "infrastructure_hub_maintenance": _("Structures"),
    "jump_clone_activation_fee": _("Fees"), "jump_clone_installation_fee": _("Fees"),
    "insurance": _("SRP / Insurance"),
}


def categorize(ref_type: str | None) -> str:
    """Human category for a ref_type (humanised fallback for unmapped ones)."""
    if not ref_type:
        return _("Other")
    if ref_type in _REF_CATEGORY:
        return _REF_CATEGORY[ref_type]
    return ref_type.replace("_", " ").title()


# --- windows -----------------------------------------------------------------
WINDOWS = {
    "7d": (_("7 days"), 7),
    "30d": (_("30 days"), 30),
    "90d": (_("90 days"), 90),
    "12m": (_("12 months"), 365),
    "ytd": (_("Year to date"), None),
    "all": (_("All time"), None),
}
DEFAULT_WINDOW = "30d"

HORIZONS = {
    "eom": _("End of month"),
    "30d": _("Next 30 days"),
    "60d": _("Next 60 days"),
    "90d": _("Next 90 days"),
}
DEFAULT_HORIZON = "30d"


_CACHE_KEY = "finance:dashboard:default:v1"
_CACHE_TTL = 1800  # 30 min; the wallet journal syncs every 6h


def default_dashboard(*, refresh: bool = False) -> dict:
    """The default view (30d · all divisions · 30d horizon), cached and warmable."""
    from django.core.cache import cache

    if not refresh:
        cached = cache.get(_CACHE_KEY)
        if cached is not None:
            return cached
    data = finance_dashboard()
    cache.set(_CACHE_KEY, data, _CACHE_TTL)
    return data


def dashboard_cached(window: str = DEFAULT_WINDOW, division: int | None = None,
                     horizon: str = DEFAULT_HORIZON, *, refresh: bool = False) -> dict:
    """``finance_dashboard`` for *any* window/division/horizon, cached at the same TTL.

    The default triple is served by the warmed ``default_dashboard``; every other
    Director-selected combination previously recomputed live on each request — this
    caches those too under a composite key so repeated views reuse the result.
    """
    from django.core.cache import cache

    key = f"corpfin:dash:{window}:{division}:{horizon}"
    if not refresh:
        cached = cache.get(key)
        if cached is not None:
            return cached
    data = finance_dashboard(window=window, division=division, horizon=horizon)
    cache.set(key, data, _CACHE_TTL)
    return data


def _granularity(days: int) -> str:
    if days <= 31:
        return "day"
    if days <= 130:
        return "week"
    return "month"


def resolve_window(key: str, now: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime, str]:
    """Return (start, end, granularity) for a window key. Unknown → default."""
    now = now or timezone.now()
    if key not in WINDOWS:
        key = DEFAULT_WINDOW
    if key == "ytd":
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    elif key == "all":
        from .models import CorpWalletJournalEntry

        first = CorpWalletJournalEntry.objects.order_by("date").values_list("date", flat=True).first()
        start = first or (now - dt.timedelta(days=30))
    else:
        start = now - dt.timedelta(days=WINDOWS[key][1])
    days = max(1, (now - start).days)
    return start, now, _granularity(days)


def _bucket(date: dt.datetime, granularity: str) -> dt.date:
    d = timezone.localtime(date).date() if timezone.is_aware(date) else date.date()
    if granularity == "day":
        return d
    if granularity == "week":
        return d - dt.timedelta(days=d.weekday())  # Monday of that week
    return d.replace(day=1)


def horizon_days(key: str, now: dt.datetime | None = None) -> int:
    """Number of days the forecast horizon covers."""
    now = now or timezone.now()
    if key == "eom":
        # First day of next month minus today.
        nxt = (now.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        return max(1, (nxt.date() - timezone.localtime(now).date()).days)
    return {"30d": 30, "60d": 60, "90d": 90}.get(key, 30)


# --- the dashboard -----------------------------------------------------------
def _current_balance(division: int | None) -> Decimal:
    from .models import CorpWalletDivision

    qs = CorpWalletDivision.objects.all()
    if division is not None:
        qs = qs.filter(division=division)
    return sum((d.balance for d in qs), start=Decimal("0"))


def _member_ids() -> set[int]:
    from apps.sso.models import EveCharacter

    from .models import CorpMember

    ids = set(EveCharacter.objects.filter(is_corp_member=True).values_list("character_id", flat=True))
    ids |= set(CorpMember.objects.values_list("character_id", flat=True))
    return ids


def _names(ids) -> dict[int, str]:
    from apps.sso.models import EveCharacter

    from .models import CorpMember, EveName

    ids = [i for i in ids if i]
    out = dict(EveName.objects.filter(entity_id__in=ids).values_list("entity_id", "name"))
    out.update(dict(CorpMember.objects.filter(character_id__in=ids).exclude(name="")
                    .values_list("character_id", "name")))
    out.update(dict(EveCharacter.objects.filter(character_id__in=ids)
                    .values_list("character_id", "name")))  # linked char names win
    return out


def finance_dashboard(window: str = DEFAULT_WINDOW, division: int | None = None,
                      horizon: str = DEFAULT_HORIZON) -> dict:
    """Everything the merged Corp Finance page needs for one window/division/horizon."""
    from .models import CorpWalletDivision, CorpWalletJournalEntry

    now = timezone.now()
    start, end, gran = resolve_window(window, now)

    entry_qs = CorpWalletJournalEntry.objects.filter(date__gte=start, date__lte=end)
    if division is not None:
        entry_qs = entry_qs.filter(division=division)
    entries = list(
        entry_qs.values("date", "ref_type", "amount", "first_party_id", "second_party_id", "tax")
    )

    member_ids = _member_ids()

    # Per-period income / expense / net.
    periods: dict[dt.date, dict] = {}
    income_cat: dict[str, Decimal] = {}
    expense_cat: dict[str, Decimal] = {}
    member_totals: dict[int, list] = {}
    tax_total = Decimal("0")
    income_total = Decimal("0")
    expense_total = Decimal("0")
    for e in entries:
        amount = e["amount"]
        b = periods.setdefault(_bucket(e["date"], gran),
                               {"income": Decimal("0"), "expense": Decimal("0")})
        cat = categorize(e["ref_type"])
        if amount > 0:
            b["income"] += amount
            income_total += amount
            income_cat[cat] = income_cat.get(cat, Decimal("0")) + amount
            member = (e["first_party_id"] if e["first_party_id"] in member_ids
                      else e["second_party_id"] if e["second_party_id"] in member_ids else None)
            if member is not None:
                agg = member_totals.setdefault(member, [Decimal("0"), 0])
                agg[0] += amount
                agg[1] += 1
        elif amount < 0:
            b["expense"] += amount
            expense_total += amount
            expense_cat[cat] = expense_cat.get(cat, Decimal("0")) + amount
        if e["tax"]:
            tax_total += e["tax"]

    ordered = sorted(periods.items())
    # Reconstruct the balance at the end of each period from the current balance
    # working backwards through net flow (accurate within our synced history).
    current_balance = _current_balance(division)
    suffix = Decimal("0")
    balance_end: dict[dt.date, Decimal] = {}
    for day, v in reversed(ordered):
        balance_end[day] = current_balance - suffix
        suffix += v["income"] + v["expense"]

    series = [
        {"label": day.isoformat(), "income": float(v["income"]), "expense": float(-v["expense"]),
         "net": float(v["income"] + v["expense"]), "balance": float(balance_end[day])}
        for day, v in ordered
    ]

    # Top earners + biggest single movements (named).
    ranked_members = sorted(member_totals.items(), key=lambda kv: -kv[1][0])[:10]
    big = sorted(entries, key=lambda e: -abs(e["amount"]))[:10]
    # Recent journal lines for the window (named, categorised).
    recent = list(
        entry_qs.order_by("-date")
        .values("date", "ref_type", "amount", "first_party_id", "second_party_id")[:60]
    )

    name_ids = {m for m, _agg in ranked_members}
    name_ids |= {e["first_party_id"] for e in big} | {e["second_party_id"] for e in big}
    name_ids |= {e["first_party_id"] for e in recent} | {e["second_party_id"] for e in recent}
    names = _names(name_ids)
    top_earners = [
        {"id": m, "name": names.get(m, f"#{m}"), "total": agg[0], "count": agg[1]}
        for m, agg in ranked_members
    ]
    movements = [
        {"date": e["date"], "amount": e["amount"], "category": categorize(e["ref_type"]),
         "ref_type": e["ref_type"],
         "party": names.get(e["second_party_id"]) or names.get(e["first_party_id"]) or ""}
        for e in big
    ]

    journal = [
        {"date": e["date"], "amount": e["amount"], "category": categorize(e["ref_type"]),
         "ref_type": e["ref_type"],
         "from": names.get(e["first_party_id"], "") if e["first_party_id"] else "",
         "to": names.get(e["second_party_id"], "") if e["second_party_id"] else ""}
        for e in recent
    ]

    net_total = income_total + expense_total
    fc = forecast(entries, horizon, current_balance, now)
    chart = {
        "series": series,
        "income_cat": [{"label": r["category"], "value": float(r["total"])}
                       for r in _cat_rows(income_cat)],
        "expense_cat": [{"label": r["category"], "value": float(r["total"])}
                        for r in _cat_rows({k: -v for k, v in expense_cat.items()})],
        "current_balance": float(current_balance),
        "forecast": fc,
    }
    return {
        "window": window, "window_label": WINDOWS.get(window, WINDOWS[DEFAULT_WINDOW])[0],
        "granularity": gran, "start": start, "division": division,
        "divisions": list(CorpWalletDivision.objects.all()),
        "current_balance": current_balance,
        "income_total": income_total, "expense_total": expense_total,
        "net_total": net_total, "tax_total": tax_total,
        "series": series,
        "income_by_category": _cat_rows(income_cat),
        "expense_by_category": _cat_rows({k: -v for k, v in expense_cat.items()}),
        "top_earners": top_earners,
        "movements": movements,
        "journal": journal,
        "chart": chart,
        "forecast": fc,
        "horizon": horizon, "horizon_label": HORIZONS.get(horizon, HORIZONS[DEFAULT_HORIZON]),
        "windows": WINDOWS, "horizons": HORIZONS,
    }


def _cat_rows(cat: dict[str, Decimal]) -> list[dict]:
    return [{"category": k, "total": v}
            for k, v in sorted(cat.items(), key=lambda kv: -kv[1]) if v]


# --- forecast ----------------------------------------------------------------
_MIN_DAYS_FOR_FORECAST = 14


def forecast(entries: list[dict], horizon: str, current_balance: Decimal,
             now: dt.datetime | None = None) -> dict:
    """Project net + balance over the horizon from the daily-net history.

    Trailing average for the central estimate; a random-walk band (σ·√days) for
    the range. Returns ``{"enough": False}`` when there's too little history to be
    honest about a projection.
    """
    now = now or timezone.now()
    days = horizon_days(horizon, now)

    # Daily net over the available history (cap to the last 90 days).
    cutoff = now - dt.timedelta(days=90)
    daily: dict[dt.date, Decimal] = {}
    for e in entries:
        if e["date"] >= cutoff:
            d = _bucket(e["date"], "day")
            daily[d] = daily.get(d, Decimal("0")) + e["amount"]
    if len(daily) < _MIN_DAYS_FOR_FORECAST:
        return {"enough": False, "horizon_days": days}

    # Fill the gap days (no entries == zero net) across the observed span.
    span_start = min(daily)
    span_end = max(daily)
    series = []
    d = span_start
    while d <= span_end:
        series.append(float(daily.get(d, Decimal("0"))))
        d += dt.timedelta(days=1)

    avg = statistics.fmean(series)
    sigma = statistics.pstdev(series) if len(series) > 1 else 0.0
    projected_net = avg * days
    band = sigma * (days ** 0.5)
    cur = float(current_balance)

    runway_days = None
    if avg < 0 and cur > 0:
        runway_days = int(cur / -avg)

    return {
        "enough": True,
        "horizon_days": days,
        "avg_daily": avg,
        "projected_net": projected_net,
        "projected_net_low": projected_net - band,
        "projected_net_high": projected_net + band,
        "projected_balance": cur + projected_net,
        "projected_balance_low": cur + projected_net - band,
        "projected_balance_high": cur + projected_net + band,
        "runway_days": runway_days,
        "trained_days": len(series),
    }
