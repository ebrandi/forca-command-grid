"""Benchmark the freight rate card against the live public courier market.

Downloads EVE Ref's public-contract snapshot, summarises every courier contract, and
jump-normalises the reward for the ones whose endpoints are NPC stations we can resolve
to systems (using our own stargate graph). Stores the summary in an AppSetting that the
freight rate-card page shows to officers.

    manage.py import_everef_contracts
"""
from __future__ import annotations

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.logistics.everef_contracts import (
    URL,
    gate_distance,
    iter_courier_contracts,
    summarise_courier_market,
)

BENCHMARK_KEY = "logistics.market_benchmark"


class Command(BaseCommand):
    help = "Benchmark the freight rate card against EVE Ref's public courier contracts."

    def handle(self, *args, **opts) -> None:
        from apps.admin_audit.models import AppSetting
        from apps.sde.models import SdeStation, SdeSystemJump

        self.stdout.write("Downloading EVE Ref public contracts…")
        try:
            resp = requests.get(
                URL, timeout=180, stream=True,
                headers={"User-Agent": settings.ESI_USER_AGENT},
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise CommandError(f"Download failed: {exc}") from exc
        from core.netcap import DataTooLarge, download_to_buffer
        try:
            buf = download_to_buffer(resp, chunk=262144)
        except DataTooLarge as exc:
            raise CommandError(f"Refused oversized download: {exc}") from exc

        rows = list(iter_courier_contracts(buf))
        if not rows:
            raise CommandError("No courier contracts found in the snapshot.")

        station_system = dict(SdeStation.objects.values_list("station_id", "system_id"))
        adjacency: dict[int, list[int]] = {}
        for f, t in SdeSystemJump.objects.values_list("from_system_id", "to_system_id"):
            adjacency.setdefault(f, []).append(t)

        def jumps_of(start: int, end: int) -> int | None:
            a, b = station_system.get(start), station_system.get(end)
            if a and b:
                return gate_distance(adjacency, a, b)
            return None

        summary = summarise_courier_market(rows, jumps_of)
        if summary is None:
            raise CommandError("No usable courier contracts.")
        summary["updated"] = timezone.now().strftime("%Y-%m-%d %H:%M UTC")

        AppSetting.objects.update_or_create(key=BENCHMARK_KEY, defaults={"value": summary})
        self.stdout.write(self.style.SUCCESS(
            f"Benchmarked {summary['count']} courier contracts "
            f"({summary['jump_sample']} jump-normalised). "
            f"Median {summary['median_reward_per_m3']} ISK/m³"
            + (f", {summary['isk_per_m3_jump']} ISK/m³/jump." if summary['isk_per_m3_jump'] else ".")
        ))
