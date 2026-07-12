"""Activity metrics for the raffle safeguards + boosters.

A **metric** is a single contest-wide number measuring how much the corp achieved
during the contest (kills, ISK destroyed, tickets issued, participating pilots…).
Leaders pick one metric to gate a *valid* draw (``min_activity``) and one to unlock
the *prize-value* booster. To help them choose realistic values, each metric can
report the home corp's **last-30-day** figure from the killboard, pro-rated to the
contest length — so a minimum can be set high enough to require real engagement but
low enough to be achievable, protecting the corp's ISK from draining on dead events.

All values are computed from the raffle's own approved ledger (what the contest
actually measured), joined to `Killmail` where a precise ISK figure is needed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.utils.translation import gettext_lazy as _


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    unit: str
    money: bool = False   # render the value as ISK
    # Whether a 30-day killboard baseline is available to suggest a value.
    has_baseline: bool = False


METRICS: list[Metric] = [
    Metric("pvp_kills", _("Valid PVP kills"), "kills", has_baseline=True),
    Metric("pvp_isk_destroyed", _("ISK destroyed (PVP)"), "ISK", money=True, has_baseline=True),
    Metric("solo_kills", _("Solo kills"), "kills", has_baseline=True),
    Metric("fleet_ops", _("Fleet attendances"), "PAPs", has_baseline=True),
    Metric("total_tickets", _("Tickets issued"), "tickets"),
    Metric("participants", _("Participating pilots"), "pilots"),
]
METRIC_BY_KEY = {m.key: m for m in METRICS}
CHOICES = [("", _("— none —"))] + [(m.key, m.label) for m in METRICS]


def get(key: str) -> Metric | None:
    return METRIC_BY_KEY.get(key)


def label(key: str) -> str:
    m = METRIC_BY_KEY.get(key)
    return m.label if m else key


def unit(key: str) -> str:
    m = METRIC_BY_KEY.get(key)
    return m.unit if m else ""


def is_money(key: str) -> bool:
    m = METRIC_BY_KEY.get(key)
    return bool(m and m.money)


# --------------------------------------------------------------------------- #
#  Current contest values
# --------------------------------------------------------------------------- #
def current_values(contest, keys=None) -> dict[str, Decimal]:
    """Current value of each requested metric for a contest (from the ledger)."""
    from django.db.models import Sum

    from apps.killboard.models import Killmail

    from .models import RaffleTicketLedgerEntry as L

    keys = set(keys) if keys else set(METRIC_BY_KEY)
    approved = L.objects.filter(contest=contest, status=L.Status.APPROVED, amount__gt=0)
    out: dict[str, Decimal] = {}

    # NOTE: .order_by() clears the model's default Meta.ordering before .distinct().
    # Without it, Django adds the ordering column (created_at) to the SELECT, so
    # DISTINCT runs over (source_ref, created_at) and one killmail with N attackers
    # (N rows, N timestamps) counts as N kills instead of 1. km_ids is also a set,
    # so a crowded kill always collapses to a single unique killmail.
    if keys & {"pvp_kills", "pvp_isk_destroyed", "solo_kills"}:
        refs = (approved.filter(source_key="pvp")
                .order_by().values_list("source_ref", flat=True).distinct())
        km_ids = {int(r.split(":", 1)[1]) for r in refs if r.startswith("killmail:")}
        if "pvp_kills" in keys:
            out["pvp_kills"] = Decimal(len(km_ids))
        if "pvp_isk_destroyed" in keys:
            v = Killmail.objects.filter(killmail_id__in=km_ids).aggregate(v=Sum("total_value"))["v"]
            out["pvp_isk_destroyed"] = Decimal(v or 0)
        if "solo_kills" in keys:
            out["solo_kills"] = Decimal(
                Killmail.objects.filter(killmail_id__in=km_ids, is_solo=True).count()
            )
    if "fleet_ops" in keys:
        out["fleet_ops"] = Decimal(
            approved.filter(source_key="fleet").order_by().values("source_ref").distinct().count()
        )
    if "total_tickets" in keys:
        out["total_tickets"] = Decimal(approved.aggregate(n=Sum("amount"))["n"] or 0)
    if "participants" in keys:
        out["participants"] = Decimal(
            approved.exclude(user_id=None).order_by().values("user_id").distinct().count()
        )
    return {k: out.get(k, Decimal("0")) for k in keys}


def value_of(contest, key: str) -> Decimal:
    if not key:
        return Decimal("0")
    return current_values(contest, [key]).get(key, Decimal("0"))


# --------------------------------------------------------------------------- #
#  30-day killboard baseline + suggestion
# --------------------------------------------------------------------------- #
def baseline_30d(key: str) -> Decimal | None:
    """The home corp's last-30-day figure for a metric, or None if unavailable."""
    from django.conf import settings

    home = int(getattr(settings, "FORCA_HOME_CORP_ID", 0) or 0)
    if not home:
        return None
    if key in ("pvp_kills", "pvp_isk_destroyed", "solo_kills"):
        from apps.killboard.models import CombatMetric

        cm = CombatMetric.objects.filter(
            entity_type=CombatMetric.EntityType.CORPORATION, entity_id=home, window="30d"
        ).first()
        if cm is None:
            return None
        return {
            "pvp_kills": Decimal(cm.kills),
            "pvp_isk_destroyed": Decimal(cm.isk_destroyed),
            "solo_kills": Decimal(cm.solo_kills),
        }[key]
    if key == "fleet_ops":
        from django.utils import timezone

        from apps.operations.models import OperationAttendance

        since = timezone.now() - timedelta(days=30)
        return Decimal(
            OperationAttendance.objects.filter(
                confirmed=True, operation__target_at__gte=since
            ).count()
        )
    return None


def kpi_panel(contest=None) -> list[dict]:
    """30-day corp KPI baselines (+ pro-rated suggestions when a contest window is
    known) to help leaders pick sensible min-activity / booster values."""
    out = []
    for m in METRICS:
        if not m.has_baseline:
            continue
        base = baseline_30d(m.key)
        if base is None:
            continue
        row = {"key": m.key, "label": m.label, "unit": m.unit, "money": m.money,
               "baseline_30d": base, "prorated": None,
               "suggested_min": None, "suggested_goal": None, "days": 0}
        if contest is not None and contest.start_at and contest.end_at:
            s = suggestion(contest, m.key)
            row.update(prorated=s["prorated"], suggested_min=s["suggested_min"],
                       suggested_goal=s["suggested_goal"], days=s["contest_days"])
        out.append(row)
    return out


def suggestion(contest, key: str) -> dict:
    """A leader-facing suggestion for a metric: the 30-day corp baseline, pro-rated
    to the contest window, with a conservative minimum (~half the pro-rated rate)
    and a booster goal (~the full pro-rated rate)."""
    base = baseline_30d(key) if key else None
    days = 0
    if contest.start_at and contest.end_at:
        days = max(1, round((contest.end_at - contest.start_at).total_seconds() / 86400))
    prorated = (base * Decimal(days) / Decimal(30)) if (base is not None and days) else None
    q = Decimal("1")
    return {
        "metric": key,
        "baseline_30d": base,
        "contest_days": days,
        "prorated": prorated.quantize(q) if prorated is not None else None,
        "suggested_min": (prorated * Decimal("0.5")).quantize(q) if prorated is not None else None,
        "suggested_goal": prorated.quantize(q) if prorated is not None else None,
    }
