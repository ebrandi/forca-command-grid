"""Services over the pilot engagement spine.

Other modules call ``record_contribution`` when a pilot completes something for
the corp; the pilot dashboard calls ``monthly_summary`` and ``recent_for_user``;
the corp recognition feed calls ``recognition_feed`` (honouring opt-out).
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import formats, timezone

from .models import ContributionEvent, PilotPreference, unit_label


def get_prefs(user) -> PilotPreference:
    """Return the member's preferences, creating defaults on first use."""
    prefs, _ = PilotPreference.objects.get_or_create(user=user)
    return prefs


def record_contribution(
    user,
    kind: str,
    magnitude=0,
    unit: str = "count",
    *,
    description: str = "",
    ref_type: str = "",
    ref_id: str = "",
    gap_ref: str = "",
    occurred_at=None,
    points=None,
) -> ContributionEvent:
    """Credit a pilot for something they did for the corp.

    Idempotent when ``ref_type``/``ref_id`` identify the source action: a retry
    updates the existing row rather than double-crediting. ``points`` defaults to
    the per-kind value from the leadership weights; kinds that need extra context
    to score (doctrine unlocks) pass an explicit pre-computed ``points``.
    """
    occurred_at = occurred_at or timezone.now()
    if points is None:
        from .weights import points_for

        points = points_for(kind, magnitude=magnitude)
    fields = {
        "user": user,
        "kind": kind,
        "magnitude": Decimal(str(magnitude)),
        "unit": unit,
        "points": int(points),
        "description": description,
        "gap_ref": gap_ref,
        "occurred_at": occurred_at,
    }
    if ref_id:
        event, _ = ContributionEvent.objects.update_or_create(
            kind=kind, ref_type=ref_type, ref_id=str(ref_id), defaults=fields
        )
        return event
    return ContributionEvent.objects.create(ref_type=ref_type, ref_id="", **fields)


def _month_start(when=None):
    now = when or timezone.now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def monthly_summary(user, when=None) -> list[dict]:
    """This-month contribution totals in native units, grouped by kind.

    Deliberately returns per-kind native totals (no composite score).
    """
    since = _month_start(when)
    rows: dict[str, dict] = {}
    qs = ContributionEvent.objects.filter(user=user, occurred_at__gte=since)
    labels = dict(ContributionEvent.Kind.choices)
    for ev in qs:
        bucket = rows.setdefault(
            ev.kind,
            {
                "kind": ev.kind,
                "label": labels.get(ev.kind, ev.kind),
                # ``unit`` is the CODE the template compares (``== 'isk'``); ``unit_label`` is
                # the translated text a human reads. Never swap one for the other.
                "unit": ev.unit,
                "unit_label": unit_label(ev.unit),
                "total": Decimal("0"),
                "points": 0,
                "count": 0,
            },
        )
        bucket["total"] += ev.magnitude
        bucket["points"] += ev.points
        bucket["count"] += 1
    return sorted(rows.values(), key=lambda r: r["label"])


def monthly_points(user, when=None) -> int:
    """The pilot's total contribution points this month."""
    from django.db.models import Sum

    since = _month_start(when)
    return (
        ContributionEvent.objects.filter(user=user, occurred_at__gte=since)
        .aggregate(p=Sum("points"))["p"]
        or 0
    )


def points_leaderboard(limit: int = 10, when=None) -> list[dict]:
    """Top contributors by points this month (respects recognition opt-out)."""
    from django.db.models import Sum

    since = _month_start(when)
    opted_out = PilotPreference.objects.filter(public_recognition=False).values_list(
        "user_id", flat=True
    )
    rows = (
        ContributionEvent.objects.filter(occurred_at__gte=since)
        .exclude(user_id__in=opted_out)
        .values("user_id")
        .annotate(points=Sum("points"))
        .filter(points__gt=0)
        .order_by("-points")[:limit]
    )
    users = {
        u.id: u
        for u in get_user_model().objects.filter(
            id__in=[r["user_id"] for r in rows]
        ).prefetch_related("characters")  # display_name per row without an N+1
    }
    out = []
    for r in rows:
        user = users.get(r["user_id"])
        if user is not None:
            out.append({"user": user, "points": r["points"]})
    return out


def recent_for_user(user, limit: int = 8) -> list[ContributionEvent]:
    return list(ContributionEvent.objects.filter(user=user)[:limit])


def _month_iter(start, end):
    """Yield the first-of-month datetimes from ``start`` month to ``end`` month inclusive."""
    import datetime as dt

    cur = _month_start(start)
    last = _month_start(end)
    while cur <= last:
        yield cur
        cur = _month_start(cur + dt.timedelta(days=32))


def personal_trend(user, months: int = 6, now=None) -> dict:
    """A pilot's *private* contribution trajectory — never comparative.

    Returns a per-kind ``months``-long monthly sparkline (native units), the pilot's
    personal-best month per kind, and their longest / current active-week streak. Reads
    only ``ContributionEvent`` for this user; there is no cross-pilot ranking anywhere in
    the result, by design (this surface motivates without leaderboard pressure).
    """
    import datetime as dt

    from django.db.models import Count, Sum
    from django.db.models.functions import TruncMonth, TruncWeek

    now = now or timezone.now()
    window_start = _month_start(now)
    for _ in range(max(1, months) - 1):
        window_start = _month_start(window_start - dt.timedelta(days=1))
    month_starts = list(_month_iter(window_start, now))
    index_of = {m.date(): i for i, m in enumerate(month_starts)}

    labels = dict(ContributionEvent.Kind.choices)
    rows = (
        ContributionEvent.objects.filter(user=user, occurred_at__gte=window_start)
        .annotate(m=TruncMonth("occurred_at", tzinfo=dt.UTC))
        .values("kind", "unit", "m")
        .annotate(total=Sum("magnitude"), points=Sum("points"), n=Count("id"))
    )
    by_kind: dict[str, dict] = {}
    for r in rows:
        k = r["kind"]
        bucket = by_kind.setdefault(k, {
            "kind": k, "label": labels.get(k, k),
            "unit": r["unit"], "unit_label": unit_label(r["unit"]),
            "series": [Decimal("0")] * len(month_starts),
            "points": [0] * len(month_starts),
        })
        idx = index_of.get(r["m"].date())
        if idx is not None:
            # ``+=`` (not ``=``): a kind ever recorded under two units in one month
            # would otherwise lose a cell. One unit per kind today, so a no-op in practice.
            bucket["series"][idx] += r["total"] or Decimal("0")
            bucket["points"][idx] += r["points"] or 0

    kinds = []
    for bucket in by_kind.values():
        series = bucket["series"]
        peak = max(series) if series else Decimal("0")
        kinds.append({
            "kind": bucket["kind"], "label": bucket["label"],
            "unit": bucket["unit"], "unit_label": bucket["unit_label"],
            "series": series, "peak": peak,
            "total": sum(series, start=Decimal("0")),
            # 0-100 bar heights relative to the pilot's own peak (private, not vs others).
            "bars": [int((v / peak) * 100) if peak else 0 for v in series],
        })
    kinds.sort(key=lambda x: x["label"])

    # Active-week streaks: consecutive ISO weeks with at least one contribution.
    # ``.order_by()`` drops the model's ``-occurred_at`` Meta ordering, which would
    # otherwise force ``occurred_at`` into the SELECT and defeat the week-level DISTINCT
    # (returning one row per event instead of one per active week).
    week_starts = sorted({
        w.date() for w in ContributionEvent.objects.filter(user=user).order_by()
        .annotate(w=TruncWeek("occurred_at", tzinfo=dt.UTC)).values_list("w", flat=True).distinct()
        if w is not None
    })
    longest = current = run = 0
    prev = None
    for wk in week_starts:
        run = run + 1 if (prev is not None and (wk - prev).days == 7) else 1
        longest = max(longest, run)
        prev = wk
    if week_starts:
        # "current" only counts if the streak reaches this week or last week.
        this_week = (now - dt.timedelta(days=now.weekday())).date()
        if (this_week - week_starts[-1]).days <= 7:
            current = run

    return {
        # date_format honours the active locale ("janv." fr, "3月" ja) while staying
        # byte-identical to the old strftime("%b") under English. Labels are frozen into
        # the json_script chart payload server-side (no JS-side date formatting).
        "month_labels": [formats.date_format(m, "M") for m in month_starts],
        "kinds": kinds,
        "longest_streak": longest,
        "current_streak": current,
        "months": len(month_starts),
    }


def corp_monthly_totals(when=None) -> list[dict]:
    """This-month contribution totals across the whole corp, grouped by kind.

    Native units (no composite score), so leadership can see what kind of work
    the corp has been doing this month — e.g. ISK mined, ships built, hauls run.
    """
    from django.db.models import Count, Sum

    since = _month_start(when)
    labels = dict(ContributionEvent.Kind.choices)
    rows = (
        ContributionEvent.objects.filter(occurred_at__gte=since)
        .values("kind", "unit")
        .annotate(total=Sum("magnitude"), points=Sum("points"), count=Count("id"))
        .order_by("kind")
    )
    out: dict[str, dict] = {}
    for r in rows:
        bucket = out.setdefault(
            r["kind"],
            {"kind": r["kind"], "label": labels.get(r["kind"], r["kind"]),
             "unit": r["unit"], "unit_label": unit_label(r["unit"]),
             "total": Decimal("0"), "points": 0, "count": 0},
        )
        bucket["total"] += r["total"] or Decimal("0")
        bucket["points"] += r["points"] or 0
        bucket["count"] += r["count"]
    return sorted(out.values(), key=lambda r: r["label"])


def recognition_feed(limit: int = 12) -> list[ContributionEvent]:
    """Recent contributions across members who allow public recognition."""
    opted_out = PilotPreference.objects.filter(public_recognition=False).values_list(
        "user_id", flat=True
    )
    return list(
        ContributionEvent.objects.exclude(user_id__in=opted_out)
        .select_related("user")
        .prefetch_related("user__characters")[:limit]  # display_name sans N+1
    )


# --- PCC-3 (3.11): constraint-aware, future-only contribution nudge ----------
# Keywords (matched against a live constraint's category + key) that a member's usual
# contribution kind can help relieve.
_KIND_HELPS = {
    "haul": ("logistics", "stock", "supply", "hauling"),
    "build": ("doctrine_stock", "stock", "supply", "industry", "production"),
    "mining": ("industry", "mineral", "ore", "stock"),
    "fleet": ("combat", "defence", "readiness", "pvp"),
    "train": ("combat", "doctrine", "readiness"),
    "doctrine": ("combat", "doctrine", "readiness"),
}


def contribution_nudge(user) -> dict | None:
    """An in-page, future-only steer: where the member's usual contribution would help a
    current binding corp constraint most (PCC-3 / 3.11), or ``None`` if nothing matches.

    Reads only the member's own recent contribution kinds and the live corp constraints — it
    is a gentle in-page prompt, never an alert, and rewrites no history. Best-effort: a
    constraint-engine hiccup returns None so the contribution page never breaks.
    """
    from datetime import timedelta

    from django.db.models import Count
    from django.utils import timezone

    since = timezone.now() - timedelta(days=90)
    kinds = list(
        ContributionEvent.objects.filter(user=user, occurred_at__gte=since)
        .values("kind").annotate(n=Count("id")).order_by("-n")
    )
    if not kinds:
        return None
    try:
        from apps.command_intel.pilot import _open_constraints
        from apps.command_intel.snapshot import latest_snapshot

        constraints = _open_constraints(latest_snapshot())
    except Exception:  # noqa: BLE001 — the contribution page must survive a CI hiccup
        return None
    if not constraints:
        return None

    labels = dict(ContributionEvent.Kind.choices)
    for k in kinds:
        keywords = _KIND_HELPS.get(k["kind"])
        if not keywords:
            continue
        for c in constraints:
            hay = f"{(c.category or '').lower()} {(c.key or '').lower()}"
            if any(w in hay for w in keywords):
                return {
                    "kind": k["kind"],
                    "kind_label": labels.get(k["kind"], k["kind"]),
                    "count_90d": k["n"],
                    "constraint_label": c.label_i18n,
                }
    return None
