"""Performance indexes for the killboard read paths.

Built CONCURRENTLY (atomic=False) so creating them on the large, still-growing
production Killmail/KillmailParticipant tables does not take a long write lock —
the only writer is the background ingestion, which keeps running during the build.
"""
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ("killboard", "0003_combatmetric_final_blows"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="killmail",
            index=models.Index(
                fields=["involves_home_corp", "home_corp_role", "-killmail_time"],
                name="km_home_role_time_idx",
            ),
        ),
        AddIndexConcurrently(
            model_name="killmail",
            index=models.Index(fields=["victim_ship_type_id"], name="km_victim_ship_idx"),
        ),
        AddIndexConcurrently(
            model_name="killmail",
            index=models.Index(fields=["victim_character_id"], name="km_victim_char_idx"),
        ),
        AddIndexConcurrently(
            model_name="killmailparticipant",
            index=models.Index(
                fields=["role", "corporation_id", "character_id"],
                name="kp_role_corp_char_idx",
            ),
        ),
    ]
