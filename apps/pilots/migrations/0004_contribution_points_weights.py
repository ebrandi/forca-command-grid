from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pilots", "0003_prune_contribution_kinds"),
    ]

    operations = [
        migrations.AddField(
            model_name="contributionevent",
            name="points",
            field=models.IntegerField(db_index=True, default=0),
        ),
        migrations.CreateModel(
            name="ContributionWeights",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(default="Standard", max_length=80)),
                ("is_active", models.BooleanField(default=True)),
                ("enabled", models.BooleanField(default=True)),
                ("task_points", models.IntegerField(default=1, help_text="Points per completed task.")),
                ("fleet_points", models.IntegerField(default=2, help_text="Points per fleet attended.")),
                ("haul_points", models.IntegerField(default=3, help_text="Points per delivered haul.")),
                ("haul_requires_verification", models.BooleanField(default=True, help_text="Only award haul points once the delivery is ESI-verified in-game.")),
                ("build_points_per_ship", models.IntegerField(default=1, help_text="Points per ship built and delivered.")),
                ("mining_points_per_mil", models.DecimalField(decimal_places=3, default=Decimal("0.100"), help_text="Points per 1,000,000 ISK of mining payout.", max_digits=8)),
                ("srp_points_per_mil", models.DecimalField(decimal_places=3, default=Decimal("0.000"), help_text="Points per 1,000,000 ISK of SRP (0 = SRP earns no points).", max_digits=8)),
                ("train_points_per_level", models.IntegerField(default=1, help_text="Points per recommended skill level trained.")),
                ("doctrine_base", models.IntegerField(default=5, help_text="Base points for unlocking any doctrine ship.")),
                ("doctrine_priority_coef", models.DecimalField(decimal_places=2, default=Decimal("0.10"), help_text="Extra points × the doctrine's corp priority (0–100).", max_digits=6)),
                ("doctrine_effort_per_mil_sp", models.DecimalField(decimal_places=2, default=Decimal("1.00"), help_text="Extra points per 1,000,000 SP the doctrine fit requires.", max_digits=6)),
            ],
            options={"abstract": False},
        ),
    ]
