"""Galactic coordinates on solar systems, for jump-freighter distance routing."""
from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sde", "0004_sdestation"),
    ]

    operations = [
        migrations.AddField(
            model_name="sdesolarsystem",
            name="x",
            field=models.FloatField(default=0.0),
        ),
        migrations.AddField(
            model_name="sdesolarsystem",
            name="y",
            field=models.FloatField(default=0.0),
        ),
        migrations.AddField(
            model_name="sdesolarsystem",
            name="z",
            field=models.FloatField(default=0.0),
        ),
    ]
