"""Add the leadership-tunable SrpProgram and the new claim snapshot fields."""
from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("srp", "0002_default_rule"),
    ]

    operations = [
        migrations.CreateModel(
            name="SrpProgram",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(default="Standard", max_length=80)),
                ("is_active", models.BooleanField(default=True)),
                ("enabled", models.BooleanField(default=True)),
                ("payout_mode", models.CharField(
                    choices=[
                        ("replacement", "Replacement ship & fit"),
                        ("isk_full", "ISK — full loss value"),
                        ("isk_topup", "ISK — top up official insurance"),
                    ],
                    default="isk_full", max_length=12)),
                ("valuation", models.CharField(
                    choices=[
                        ("actual", "Actual loss (hull + destroyed modules)"),
                        ("doctrine", "Doctrine fit value"),
                        ("hull", "Hull only"),
                    ],
                    default="doctrine", max_length=10)),
                ("default_cap", models.DecimalField(decimal_places=2, default=0, max_digits=20)),
                ("require_doctrine", models.BooleanField(
                    default=True,
                    help_text="Only losses flying an active doctrine hull are eligible.")),
                ("cover_pod", models.BooleanField(
                    default=False, help_text="Also cover capsule (pod) losses.")),
                ("insurance_fraction", models.DecimalField(
                    decimal_places=3, default=Decimal("0.400"), max_digits=4)),
                ("intro_text", models.TextField(
                    blank=True,
                    default=(
                        "Lose a doctrine ship on a fleet op and the corp helps you "
                        "replace it. Submit a claim from your eligible losses below "
                        "and an officer reviews it."
                    ))),
            ],
            options={
                "ordering": ["-is_active", "-updated_at"],
            },
        ),
        migrations.AddField(
            model_name="srpclaim",
            name="payout_mode",
            field=models.CharField(
                choices=[
                    ("replacement", "Replacement ship & fit"),
                    ("isk_full", "ISK — full loss value"),
                    ("isk_topup", "ISK — top up official insurance"),
                ],
                default="isk_full", max_length=12),
        ),
        migrations.AddField(
            model_name="srpclaim",
            name="loss_value",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=20),
        ),
        migrations.AddField(
            model_name="srpclaim",
            name="insurance_estimate",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=20),
        ),
        migrations.AddField(
            model_name="srpclaim",
            name="approved_payout",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name="srpclaim",
            name="payment_reference",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
