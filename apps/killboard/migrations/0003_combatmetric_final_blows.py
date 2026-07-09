"""Add final_blows to the per-pilot combat rollup so pilot_combat_card can be
served from the precomputed row instead of live-aggregating participants."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("killboard", "0002_fix_battle_report_titles"),
    ]

    operations = [
        migrations.AddField(
            model_name="combatmetric",
            name="final_blows",
            field=models.IntegerField(default=0),
        ),
    ]
