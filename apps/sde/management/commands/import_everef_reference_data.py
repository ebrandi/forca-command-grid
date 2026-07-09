"""Populate SdeType.packaged_volume from EVE Ref's reference-data bundle.

Downloads the (~14 MB) ``reference-data-latest.tar.xz`` and copies each type's
``packaged_volume`` onto the matching SdeType row. Used to size jump-freight loads in
the supply forecast with real repackaged volumes instead of a per-class approximation.

    manage.py import_everef_reference_data
"""
from __future__ import annotations

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.sde.everef_refdata import iter_type_volumes
from apps.sde.models import SdeType

URL = "https://data.everef.net/reference-data/reference-data-latest.tar.xz"


class Command(BaseCommand):
    help = "Set SdeType.packaged_volume from EVE Ref reference-data."

    def handle(self, *args, **opts) -> None:
        self.stdout.write("Downloading EVE Ref reference-data…")
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

        volumes = dict(iter_type_volumes(buf))
        self.stdout.write(f"  parsed {len(volumes)} packaged volumes; matching to our types…")

        existing = list(SdeType.objects.only("type_id", "packaged_volume"))
        updates = []
        for t in existing:
            pv = volumes.get(t.type_id)
            if pv is not None and t.packaged_volume != pv:
                t.packaged_volume = pv
                updates.append(t)
        SdeType.objects.bulk_update(updates, ["packaged_volume"], batch_size=2000)
        self.stdout.write(self.style.SUCCESS(f"Updated packaged_volume on {len(updates)} types."))
