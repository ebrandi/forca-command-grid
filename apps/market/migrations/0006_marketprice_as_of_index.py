"""Freshness index for the "market prices last refreshed" health probe.

``apps/admin_audit/health.feed_health`` reads the newest ``as_of`` off MarketPrice.
Unlike the other feeds it has no ``sync:`` stamp to short-circuit it, so the query runs
on every 120-second health recompute and previously sorted one row per priced type ×
location × profile to return a single value.

Built CONCURRENTLY (``atomic = False``): the price import rewrites this table wholesale
on its beat, and a plain ``ADD INDEX`` would take an ACCESS EXCLUSIVE lock that stalls
(or is stalled by) a running import. The table is only tens of thousands of rows, so
expect a few seconds. Reversible: the backwards direction drops the index, also
concurrently.
"""
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ("market", "0005_alter_markethistory_source_and_more"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="marketprice",
            index=models.Index(fields=["-as_of"], name="mktprice_as_of_idx"),
        ),
    ]
