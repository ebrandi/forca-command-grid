"""G5 — DoctrineReadinessConfig (hand-authored, additive; doc 07 §3.3)."""
from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("readiness", "0006_config_tables"),
        ("doctrines", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="DoctrineReadinessConfig",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("is_primary", models.BooleanField(default=False)),
                ("is_mandatory", models.BooleanField(default=False)),
                ("is_alliance", models.BooleanField(default=False)),
                ("retirement_date", models.DateField(blank=True, null=True)),
                ("min_pilots", models.PositiveIntegerField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("doctrine", models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="readiness_config", to="doctrines.doctrine")),
            ],
        ),
    ]
