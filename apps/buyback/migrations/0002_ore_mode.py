from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("buyback", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="buybackconfig",
            name="ore_mode_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="buybackconfig",
            name="reprocessing_pct",
            field=models.DecimalField(
                decimal_places=3, default=Decimal("0.906"), max_digits=4,
                help_text="Effective refine yield (skills + structure + rig) used to value ore.",
            ),
        ),
    ]
