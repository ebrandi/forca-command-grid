#!/usr/bin/env python
"""Trigger every cache warmer once — a cold-start smoke + a post-Redis-flush re-warm.

Run inside the app container:
    docker compose exec web python scripts/perf/warm_all_caches.py

Idempotent and safe: each warmer reads from the local DB and writes its Redis cache; none
call external APIs here. Prints per-warmer status + timing so it doubles as a smoke test.
Use after a deploy that flushed Redis so the first real visitor doesn't pay the cold cost.
"""
from __future__ import annotations

import os
import sys
import time

# Make the project root importable so `config` resolves however the script is invoked.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import django  # noqa: E402

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

# (label, dotted path to a zero-arg callable). These mirror the `warm-*` beat tasks.
WARMERS = [
    ("killboard", "apps.killboard.tasks.warm_caches"),
    ("hall_of_fame", "apps.pilots.tasks.warm_hall_of_fame"),
    ("pilot_briefings", "apps.pilots.tasks.warm_briefings"),
    ("readiness", "apps.readiness.tasks.warm_readiness"),
    ("readiness_pilots", "apps.readiness.tasks.warm_pilots"),
    ("market_dashboard", "apps.market.tasks.warm_dashboard"),
    ("finance_dashboard", "apps.corporation.tasks.warm_finance_dashboard"),
    ("map_overlays", "apps.navigation.tasks.warm_map_overlays"),
]


def _resolve(path: str):
    module, _, attr = path.rpartition(".")
    import importlib

    return getattr(importlib.import_module(module), attr)


def main() -> None:
    # Also warm the newly-cached universe map (no dedicated task).
    from apps.navigation.maps import universe_map

    extra = [("universe_map", universe_map)]

    for label, path in WARMERS:
        try:
            fn = _resolve(path)
        except (ImportError, AttributeError) as exc:
            print(f"  SKIP  {label:18} ({exc})")
            continue
        t0 = time.perf_counter()
        try:
            result = fn()
            dt = (time.perf_counter() - t0) * 1000
            print(f"  OK    {label:18} {dt:7.0f} ms  -> {result!r:.60}")
        except Exception as exc:  # noqa: BLE001 - a smoke run surfaces, never crashes on one
            print(f"  FAIL  {label:18} {type(exc).__name__}: {exc}")

    for label, fn in extra:
        t0 = time.perf_counter()
        try:
            fn()
            print(f"  OK    {label:18} {(time.perf_counter() - t0) * 1000:7.0f} ms")
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {label:18} {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
