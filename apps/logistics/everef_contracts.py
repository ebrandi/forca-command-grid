"""Benchmark our freight rates against the live public courier market.

EVE Ref's public-contract snapshot (``public-contracts-latest.v2.tar.bz2``) carries
every public courier contract — reward, collateral, volume and the start/end location.
We summarise the market and, for the contracts whose endpoints are NPC stations we can
resolve to systems, jump-normalise the reward using our own stargate graph — so the
freight officer can see how the corp's rate card compares.

Pure parsing/maths — no network, no DB — so it's easy to unit-test; the command
``import_everef_contracts`` does the downloading and storing.
"""
from __future__ import annotations

import csv
import statistics
import tarfile
from collections import deque
from collections.abc import Callable, Iterator
from typing import BinaryIO

URL = "https://data.everef.net/public-contracts/public-contracts-latest.v2.tar.bz2"


def iter_courier_contracts(fileobj: BinaryIO) -> Iterator[dict]:
    """Yield ``{reward, collateral, volume, start, end}`` for each courier contract."""
    from core.netcap import MAX_DECOMPRESSED_BYTES, DataTooLarge, capped_text

    with tarfile.open(fileobj=fileobj, mode="r:bz2") as tar:
        member = next((m for m in tar.getmembers() if m.name.endswith("contracts.csv")), None)
        if member is None:
            return
        if member.size and member.size > MAX_DECOMPRESSED_BYTES:
            raise DataTooLarge("contracts.csv exceeded the decompressed ceiling")
        handle = tar.extractfile(member)
        if handle is None:
            return
        text = capped_text(handle)
        for row in csv.DictReader(text):
            if row.get("type") != "courier":
                continue
            try:
                reward = float(row.get("reward") or 0)
                volume = float(row.get("volume") or 0)
                collateral = float(row.get("collateral") or 0)
            except (ValueError, TypeError):
                continue
            yield {
                "reward": reward, "volume": volume, "collateral": collateral,
                "start": _int(row.get("start_location_id")),
                "end": _int(row.get("end_location_id")),
            }


def _int(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def gate_distance(adjacency: dict[int, list[int]], origin: int, dest: int,
                  max_hops: int = 60) -> int | None:
    """Fewest stargate jumps between two systems (BFS over the local gate graph)."""
    if origin == dest:
        return 0
    seen = {origin}
    queue: deque[tuple[int, int]] = deque([(origin, 0)])
    while queue:
        node, dist = queue.popleft()
        if dist >= max_hops:
            continue
        for nb in adjacency.get(node, ()):
            if nb == dest:
                return dist + 1
            if nb not in seen:
                seen.add(nb)
                queue.append((nb, dist + 1))
    return None


def summarise_courier_market(rows: list[dict], jumps_of: Callable[[int, int], int | None]) -> dict | None:
    """Market summary; ``jumps_of(start, end)`` gives stargate jumps or None.

    Reward-per-m³ and collateral % use every contract; the jump-normalised
    ISK/m³/jump uses only those whose route we can resolve.
    """
    rewards, vols, per_m3, coll_pct, per_m3_jump = [], [], [], [], []
    for r in rows:
        reward, vol, coll = r["reward"], r["volume"], r["collateral"]
        if reward <= 0 or vol <= 0:
            continue
        rewards.append(reward)
        vols.append(vol)
        per_m3.append(reward / vol)
        if coll > 0:
            coll_pct.append(reward / coll * 100)
        if r["start"] and r["end"]:
            j = jumps_of(r["start"], r["end"])
            if j and j >= 1:
                per_m3_jump.append(reward / (vol * j))
    if not rewards:
        return None
    med = statistics.median
    return {
        "count": len(rewards),
        "median_reward": round(med(rewards)),
        "median_volume": round(med(vols)),
        "median_reward_per_m3": round(med(per_m3), 2),
        "median_collateral_pct": round(med(coll_pct), 2) if coll_pct else None,
        "isk_per_m3_jump": round(med(per_m3_jump), 2) if per_m3_jump else None,
        "jump_sample": len(per_m3_jump),
    }
