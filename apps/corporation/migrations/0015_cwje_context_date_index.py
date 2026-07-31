"""Partial composite index backing the P4 payment reconcile's context_id lookup.

``apps/procurement/payments.reconcile_payments`` finds a purchase order's payment by
``context_id == contract_id``, oldest qualifying row first. Nothing indexed context_id,
so every candidate PO cost a full scan of the corp wallet journal plus a top-N sort —
once per PO, per reconcile beat. ``(context_id, date)`` puts the equality predicate
first and the range/ORDER BY second, so the LIMIT 1 stops on the first index row.

Partial on ``context_id IS NOT NULL``: ESI context ids are recorded on new journal rows
only (historical rows stay null and are never backfilled), so the great majority of the
table is excluded and the index stays small.

Built CONCURRENTLY (``atomic = False``) because the journal grows without bound and the
finance sync writes to it continuously. Expect a few seconds on the current production
journal — CONCURRENTLY makes two passes and is slower than a plain build on the HDD
RAID, but it never blocks reads or writes. Reversible: the backwards direction drops
the index, also concurrently.
"""
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ("corporation", "0014_alter_contact_source_alter_corpmember_source_and_more"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="corpwalletjournalentry",
            index=models.Index(
                condition=models.Q(("context_id__isnull", False)),
                fields=["context_id", "date"],
                name="cwje_context_date_idx",
            ),
        ),
    ]
