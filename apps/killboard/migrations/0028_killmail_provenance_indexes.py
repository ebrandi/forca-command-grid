"""Freshness indexes for the director dashboard's integration-health panel.

``apps/admin_audit/health.feed_health`` reports killmail freshness as the newest
``fetched_at``, falling back to the newest ``as_of``. Neither column was indexed, so
both probes scanned the whole Killmail table and top-N sorted it. Nothing in the ingest
path ever writes ``fetched_at``, which means the fallback is the branch that actually
runs — the expensive one — on every 120-second health recompute.

``km_fetched_at_idx`` is partial on ``fetched_at IS NOT NULL``: today that makes it an
empty index that answers the first probe instantly, and it stays correct (and nearly
free to maintain) if ingestion ever starts stamping the column. ``km_as_of_idx`` is the
one that carries real weight; ``as_of`` is stamped once at ingest and never rewritten,
so it is append-mostly.

Both built CONCURRENTLY (``atomic = False``) — the production Killmail table is ~180k
rows and still growing, and background ingestion writes to it around the clock. On the
HDD RAID expect roughly a minute for the ``as_of`` build (the partial one is
instantaneous while ``fetched_at`` is universally null); neither blocks readers or
writers, matching 0006/0010. Reversible: the backwards direction drops both indexes,
also concurrently.
"""
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ("doctrines", "0008_remove_doctrine_is_public_preview_and_more"),
        ("killboard", "0027_seed_signature_backgrounds"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="killmail",
            index=models.Index(
                condition=models.Q(("fetched_at__isnull", False)),
                fields=["-fetched_at"],
                name="km_fetched_at_idx",
            ),
        ),
        AddIndexConcurrently(
            model_name="killmail",
            index=models.Index(fields=["-as_of"], name="km_as_of_idx"),
        ),
    ]
