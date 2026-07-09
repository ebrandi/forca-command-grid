"""Indexes for the killboard alliance + attacker-side filters (KB-03).

Built CONCURRENTLY (atomic=False) so adding them to the large, still-growing
production Killmail/KillmailParticipant tables does not take a long write lock —
the only writer is the background ingestion, which keeps running during the build.
"""
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ("killboard", "0005_killfeed"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="killmail",
            index=models.Index(fields=["victim_alliance_id"], name="km_victim_alliance_idx"),
        ),
        AddIndexConcurrently(
            model_name="killmailparticipant",
            index=models.Index(fields=["role", "alliance_id"], name="kp_role_alliance_idx"),
        ),
    ]
