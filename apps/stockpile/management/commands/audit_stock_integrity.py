"""Pre-migration data audit for the P1 stock-integrity constraints.

Run BEFORE applying the stockpile CheckConstraint migration on any real database
(dev/test/prod): Django validates existing rows when a constraint is added, so a
violating row fails the migration mid-deploy. This command names every violation
so it can be fixed first, and reports the stranded ACTIVE reservations that the
release data migration will close.

Read-only. Exits non-zero (CommandError) when a constraint-violating row exists.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.stockpile.models import StockpileItem, StockReservation


class Command(BaseCommand):
    help = "Audit stockpile rows against the P1 integrity constraints (read-only)."

    def handle(self, *args, **options):
        negative = list(
            StockpileItem.objects.filter(quantity_current__lt=0)
            .values_list("pk", "stockpile_id", "type_id", "quantity_current")
        )
        subunit = list(
            StockReservation.objects.filter(quantity_reserved__lt=1)
            .values_list("pk", "stockpile_item_id", "quantity_reserved", "status")
        )
        stranded = list(
            StockReservation.objects.filter(
                status=StockReservation.Status.ACTIVE,
                project__status__in=["done", "cancelled"],
            ).values_list("pk", "project_id", "quantity_reserved")
        )

        for pk, sp, tid, qty in negative:
            self.stdout.write(f"NEGATIVE StockpileItem pk={pk} stockpile={sp} type={tid} quantity_current={qty}")
        for pk, item, qty, status in subunit:
            self.stdout.write(f"SUBUNIT StockReservation pk={pk} item={item} quantity_reserved={qty} status={status}")
        for pk, project, qty in stranded:
            self.stdout.write(f"STRANDED StockReservation pk={pk} project={project} quantity_reserved={qty}")

        self.stdout.write(
            f"negative_items={len(negative)} subunit_reservations={len(subunit)} "
            f"stranded_active_on_closed_projects={len(stranded)}"
        )
        if negative or subunit:
            raise CommandError(
                "constraint-violating rows found — fix them before applying the "
                "stockpile constraint migration"
            )
        self.stdout.write(self.style.SUCCESS(
            "OK — constraints will apply cleanly"
            + ("" if not stranded else " (stranded reservations will be released by the data migration)")
        ))
