"""Bulk-import Jita (The Forge) market history from EVE Ref's daily archives.

One download per day covers every type, so this fills MarketHistory far more completely
than the per-type ESI history sync (which is capped at ~120 tracked types). By default
it imports only the types we actually surface (priced types, doctrine hulls, killmail
ships); pass --all for every type in the region.

Powers price trends/sparklines (apps/market price_trend) across the market dashboard,
the store and the supply forecast, and lays the groundwork for valuing historical
killmails at period-accurate prices.

Usage:
    manage.py import_everef_market_history --days 365            # last year, tracked types
    manage.py import_everef_market_history --from 2024-01-01 --to 2024-06-30
    manage.py import_everef_market_history --days 90 --all       # every Forge type
"""
from __future__ import annotations

import datetime as dt
import io
import time
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.market.everef_history import THE_FORGE, day_url, iter_region_rows
from apps.market.models import MarketHistory
from core.mixins import Source


def _parse_date(value: str) -> dt.date:
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise CommandError(f"Invalid date {value!r}; use YYYY-MM-DD.") from exc


def _dec(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _tracked_types() -> set[int]:
    from apps.doctrines.models import DoctrineFit
    from apps.killboard.models import Killmail
    from apps.market.models import MarketPrice

    ids = set(MarketPrice.objects.values_list("type_id", flat=True).distinct())
    ids |= set(DoctrineFit.objects.values_list("ship_type_id", flat=True))
    ids |= set(Killmail.objects.values_list("victim_ship_type_id", flat=True).distinct())
    ids.discard(None)
    ids.discard(0)
    return ids


class Command(BaseCommand):
    help = "Bulk-import Jita market history from EVE Ref's daily archives."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--from", dest="from_date", default=None, help="start date YYYY-MM-DD")
        parser.add_argument("--to", dest="to_date", default=None, help="end date YYYY-MM-DD (default: today UTC)")
        parser.add_argument("--days", type=int, default=90, help="if --from omitted, import the last N days")
        parser.add_argument("--all", action="store_true", help="import every type, not just tracked ones")
        parser.add_argument("--day-delay", type=float, default=0.0, help="seconds to pause between days")

    def handle(self, *args, **opts) -> None:
        end = _parse_date(opts["to_date"]) if opts["to_date"] else dt.datetime.now(dt.UTC).date()
        if opts["from_date"]:
            start = _parse_date(opts["from_date"])
        else:
            start = end - dt.timedelta(days=max(1, opts["days"]) - 1)
        if start > end:
            raise CommandError("--from must be on or before --to.")

        types = None if opts["all"] else _tracked_types()
        if types is not None and not types:
            raise CommandError("No tracked types found; load data first or pass --all.")
        delay = max(0.0, opts["day_delay"])

        session = requests.Session()
        session.headers["User-Agent"] = settings.ESI_USER_AGENT
        scope = "all types" if types is None else f"{len(types)} tracked types"
        self.stdout.write(
            f"Importing The Forge market history {start}→{end} "
            f"({(end - start).days + 1} days, {scope})."
        )

        from django.utils import timezone
        days = rows_written = errors = 0
        day = start
        while day <= end:
            days += 1
            try:
                blob = self._download(session, day)
            except Exception as exc:  # noqa: BLE001 - network hiccup: log and continue
                errors += 1
                self.stderr.write(f"  {day}: download failed ({exc}); skipping.")
                day += dt.timedelta(days=1)
                continue

            if blob is not None:
                now = timezone.now()
                objs = []
                for r in iter_region_rows(blob, type_ids=types):
                    try:
                        d = dt.datetime.strptime(r["date"], "%Y-%m-%d").date()
                    except (TypeError, ValueError):
                        continue
                    objs.append(MarketHistory(
                        type_id=r["type_id"], region_id=THE_FORGE, date=d,
                        average=_dec(r["average"]), highest=_dec(r["highest"]),
                        lowest=_dec(r["lowest"]), volume=r["volume"],
                        order_count=r["order_count"], source=Source.EVEREF, as_of=now,
                    ))
                if objs:
                    MarketHistory.objects.bulk_create(
                        objs, update_conflicts=True,
                        unique_fields=["type_id", "region_id", "date"],
                        update_fields=["average", "highest", "lowest", "volume",
                                       "order_count", "source", "as_of"],
                    )
                    rows_written += len(objs)

            if days % 30 == 0:
                self.stdout.write(f"  …{day} · {days} days · {rows_written} rows")
            if delay:
                time.sleep(delay)
            day += dt.timedelta(days=1)

        self.stdout.write(self.style.SUCCESS(
            f"Done: {days} days, {rows_written} day-rows written"
            + (f", {errors} errors" if errors else "")
        ))

    def _download(self, session: requests.Session, day: dt.date) -> io.BytesIO | None:
        url = day_url(day)
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = session.get(url, timeout=120, stream=True)
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                last_exc = RuntimeError(f"HTTP {resp.status_code}")
                time.sleep(1.5 * (attempt + 1))
                continue
            from core.netcap import download_to_buffer
            return download_to_buffer(resp, chunk=131072)
        raise last_exc or RuntimeError("download failed")
