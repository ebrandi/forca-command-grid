"""Price every type referenced by the corp's data, then re-value & recompute.

Pulls Jita aggregates from Fuzzwork for all type ids referenced by killmails,
projects, hauling, stock and PI, stores them as MarketPrice, then re-values
killmails and recomputes industry BOMs so ISK figures are populated. This is the
manual, on-demand form of the scheduled ``market.sync_jita_prices`` beat task —
both share the ``refresh_jita_prices`` / ``revalue_from_prices`` services.

    manage.py price_types
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.killboard.models import Killmail
from apps.market.services import refresh_jita_prices, revalue_from_prices


class Command(BaseCommand):
    help = "Price all referenced types from Fuzzwork and re-value/recompute."

    def handle(self, *args, **options) -> None:
        priced = refresh_jita_prices()
        stats = revalue_from_prices()
        self.stdout.write(self.style.SUCCESS(
            f"Priced {priced} types; re-valued {Killmail.objects.count()} killmails; "
            f"recomputed {stats['projects']} projects."
        ))
