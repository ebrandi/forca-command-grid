"""Partial composite index backing "most valuable kills" ORDER BY total_value DESC.

The rankings' most-valuable list (and the killfeed's biggest-kills rail) order the
home-corp, non-NPC PvP kills by ``total_value`` descending. On cold windows (the
"all" tab and historical years) that sort had no supporting index and scanned the
whole PvP-kill set. This partial composite lets Postgres seek by ``home_corp_role``
and read ``total_value`` already ordered.

Built CONCURRENTLY (atomic=False) so adding it to the large, still-growing
production Killmail table does not take a long write lock — the only writer is the
background ingestion, which keeps running during the build (matches 0006).
"""
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ("killboard", "0009_combat_ranks_rewards"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="killmail",
            index=models.Index(
                fields=["home_corp_role", "-total_value"],
                name="km_role_value_idx",
                condition=models.Q(involves_home_corp=True, is_npc=False),
            ),
        ),
    ]
