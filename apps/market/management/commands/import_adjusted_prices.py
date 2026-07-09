"""Refresh CCP daily reference prices from the public ESI ``/markets/prices/``.

These ``ADJUSTED``-profile rows are the price fallback used by ``price_for`` for
any type not currently on the Jita market, replacing the bogus SDE ``base_price``.

    manage.py import_adjusted_prices
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.market.services import ingest_adjusted_prices


class Command(BaseCommand):
    help = "Import CCP adjusted/average reference prices from ESI /markets/prices/."

    def handle(self, *args, **options) -> None:
        count = ingest_adjusted_prices()
        self.stdout.write(self.style.SUCCESS(f"Upserted {count} adjusted reference prices."))
