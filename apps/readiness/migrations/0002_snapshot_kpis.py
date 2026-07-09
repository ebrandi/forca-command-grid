"""Phase 0 — additive snapshot columns for the configurable engine.

Hand-authored (the container can't ``makemigrations``). Pure ``AddField`` with
defaults, so existing ``ReadinessSnapshot`` rows stay valid and the old code that
ignores these columns keeps working. No data migration, fully reversible.
"""
from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("readiness", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="readinesssnapshot",
            name="kpis",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="readinesssnapshot",
            name="weights",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="readinesssnapshot",
            name="config_version",
            field=models.IntegerField(default=0),
        ),
    ]
