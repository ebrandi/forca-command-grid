"""Read EVE Ref's daily market-history archives (one CSV per day, all regions).

EVE Ref publishes daily files at
``data.everef.net/market-history/{YYYY}/market-history-{YYYY-MM-DD}.csv.bz2`` with
columns ``average,date,highest,lowest,order_count,volume,http_last_modified,region_id,
type_id`` — every type in every region for that day. We keep The Forge (Jita) rows, so
one download replaces hundreds of per-type ESI history calls.

Pure parsing — no network, no DB — so it's easy to unit-test; the command
``import_everef_market_history`` does the downloading and upserting.
"""
from __future__ import annotations

import bz2
import csv
import datetime as dt
from collections.abc import Iterator
from typing import BinaryIO

THE_FORGE = 10000002  # Jita's region — the corp's price reference
BASE = "https://data.everef.net/market-history"


def day_url(day: dt.date) -> str:
    return f"{BASE}/{day:%Y}/market-history-{day:%Y-%m-%d}.csv.bz2"


def iter_region_rows(
    fileobj: BinaryIO, *, region_id: int = THE_FORGE, type_ids: set[int] | None = None
) -> Iterator[dict]:
    """Yield one normalised dict per market-history row for ``region_id``.

    ``fileobj`` is the bz2-compressed CSV. ``type_ids`` optionally restricts output to
    the types we care about (cheap int check), keeping the import bounded.
    """
    from core.netcap import capped_text

    text = capped_text(bz2.BZ2File(fileobj))
    for row in csv.DictReader(text):
        try:
            if int(row["region_id"]) != region_id:
                continue
            tid = int(row["type_id"])
        except (KeyError, ValueError, TypeError):
            continue
        if type_ids is not None and tid not in type_ids:
            continue
        yield {
            "type_id": tid,
            "date": row.get("date"),
            "average": row.get("average"),
            "highest": row.get("highest"),
            "lowest": row.get("lowest"),
            "volume": int(row.get("volume") or 0),
            "order_count": int(row.get("order_count") or 0),
        }
