"""P1 data migration: release reservations stranded on closed projects.

Before P1 nothing consumed or released a project's reservations when it closed, so
DONE/CANCELLED projects kept ACTIVE claims on corp stock forever. The new lifecycle
(consume on delivery, release on close) prevents new strays; this converges
existing databases on the invariant at deploy time instead of lazily.

Forward: mark them RELEASED. Reverse: no-op — the claims were wrong to begin with.
Run ``manage.py audit_stock_integrity`` first to see what will be released.
"""
from __future__ import annotations

from django.db import migrations


def release_stranded(apps, schema_editor):
    StockReservation = apps.get_model("stockpile", "StockReservation")
    ids = list(
        StockReservation.objects.filter(
            status="active", project__status__in=["done", "cancelled"]
        ).values_list("pk", flat=True)
    )
    if not ids:
        return
    StockReservation.objects.filter(pk__in=ids).update(status="released")
    # Audit the release (§8.3 disclosure): system actor, ids capped for row size.
    AuditLog = apps.get_model("admin_audit", "AuditLog")
    AuditLog.objects.create(
        actor=None,
        action="stockpile.release_stranded_reservations",
        target_type="migration",
        target_id="stockpile.0005",
        metadata={"released": len(ids), "reservation_ids": ids[:200]},
    )


class Migration(migrations.Migration):

    dependencies = [
        ("stockpile", "0004_stockpileitem_stockpile_s_type_id_4a3198_idx_and_more"),
        ("admin_audit", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(release_stranded, migrations.RunPython.noop),
    ]
