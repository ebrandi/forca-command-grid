"""EVE Ref public-contract courier parsing + freight-market benchmark maths."""
from __future__ import annotations

import csv
import io
import tarfile

from apps.logistics.everef_contracts import (
    gate_distance,
    iter_courier_contracts,
    summarise_courier_market,
)

_COLS = ["collateral", "contract_id", "date_expired", "date_issued", "days_to_complete",
         "end_location_id", "issuer_corporation_id", "issuer_id", "price", "reward",
         "start_location_id", "title", "type", "volume", "http_last_modified", "region_id",
         "station_id", "system_id", "constellation_id", "for_corporation", "buyout"]


def _row(type_, reward, volume, collateral, start, end):
    d = dict.fromkeys(_COLS, "")
    d.update(type=type_, reward=reward, volume=volume, collateral=collateral,
             start_location_id=start, end_location_id=end)
    return d


def _archive(rows) -> io.BytesIO:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_COLS)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    data = buf.getvalue().encode()
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:bz2") as tar:
        info = tarfile.TarInfo("contracts.csv")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    out.seek(0)
    return out


def test_iter_courier_only():
    arch = _archive([
        _row("courier", 3000000, 1000, 18000000, 60000001, 60000002),
        _row("item_exchange", 5, 0, 0, "", ""),
    ])
    got = list(iter_courier_contracts(arch))
    assert len(got) == 1
    assert got[0]["reward"] == 3000000.0 and got[0]["start"] == 60000001


def test_gate_distance():
    adj = {1: [2], 2: [1, 3], 3: [2]}
    assert gate_distance(adj, 1, 1) == 0
    assert gate_distance(adj, 1, 3) == 2
    assert gate_distance(adj, 1, 99) is None


def test_summarise_courier_market():
    rows = [
        {"reward": 3000000, "volume": 1000, "collateral": 18000000, "start": 1, "end": 3},
        {"reward": 1000000, "volume": 500, "collateral": 10000000, "start": 1, "end": 1},
    ]

    def jumps(a, b):
        return 0 if a == b else {(1, 3): 2}.get((a, b))

    s = summarise_courier_market(rows, jumps)
    assert s["count"] == 2
    assert s["median_reward_per_m3"] == 2500.0          # median of 3000, 2000
    assert s["isk_per_m3_jump"] == 1500.0               # 3M/(1000·2); same-system skipped
    assert s["jump_sample"] == 1


def test_summarise_empty():
    assert summarise_courier_market([], lambda a, b: 1) is None
