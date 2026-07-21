"""KB-35: point-in-time at-kill valuation fields on Killmail (additive, nullable)."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("killboard", "0021_combatcampaign"),
    ]

    operations = [
        migrations.AddField(
            model_name="killmail",
            name="value_at_kill",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=20, null=True
            ),
        ),
        migrations.AddField(
            model_name="killmail",
            name="value_source",
            field=models.CharField(blank=True, default="", max_length=24),
        ),
    ]
