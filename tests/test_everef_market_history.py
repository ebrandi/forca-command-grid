"""EVE Ref market-history: CSV.bz2 parsing + the bulk import command."""
from __future__ import annotations

import bz2
import csv
import io
from decimal import Decimal

import pytest

from apps.market.everef_history import iter_region_rows

_COLS = ["average", "date", "highest", "lowest", "order_count", "volume",
         "http_last_modified", "region_id", "type_id"]


def _row(type_id, region_id, average, date="2024-06-15"):
    return {"average": str(average), "date": date, "highest": str(average), "lowest": str(average),
            "order_count": "5", "volume": "1000", "http_last_modified": "x",
            "region_id": str(region_id), "type_id": str(type_id)}


def _archive(rows) -> io.BytesIO:
    """Build a market-history-*.csv.bz2 like EVE Ref's."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_COLS)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return io.BytesIO(bz2.compress(buf.getvalue().encode()))


def test_iter_keeps_the_forge_only():
    rows = [_row(587, 10000002, 100), _row(16227, 10000002, 200), _row(587, 10000043, 9)]
    got = {r["type_id"] for r in iter_region_rows(_archive(rows))}
    assert got == {587, 16227}  # Domain (10000043) row dropped


def test_iter_type_filter():
    rows = [_row(587, 10000002, 100), _row(16227, 10000002, 200)]
    got = [r["type_id"] for r in iter_region_rows(_archive(rows), type_ids={587})]
    assert got == [587]
    sample = next(iter(iter_region_rows(_archive(rows), type_ids={587})))
    assert sample["average"] == "100" and sample["volume"] == 1000


@pytest.mark.django_db
def test_import_command_upserts(monkeypatch):
    from django.core.management import call_command

    import apps.market.management.commands.import_everef_market_history as mod
    from apps.market.models import MarketHistory, MarketPrice

    # A tracked type so the default (non --all) run has a non-empty type set.
    MarketPrice.objects.create(type_id=587, profile=MarketPrice.Profile.JITA_SELL,
                               sell_min=Decimal("100"))
    monkeypatch.setattr(
        mod.Command, "_download",
        lambda self, session, day: _archive([_row(587, 10000002, 123),
                                             _row(587, 10000043, 9)]),
    )
    call_command("import_everef_market_history", "--from", "2024-06-15", "--to", "2024-06-15")

    mh = MarketHistory.objects.get(type_id=587, region_id=10000002)
    assert mh.average == Decimal("123") and str(mh.date) == "2024-06-15"
    assert mh.source == "everef"
    # Domain row was filtered out (tracked-but-wrong-region).
    assert not MarketHistory.objects.filter(region_id=10000043).exists()
