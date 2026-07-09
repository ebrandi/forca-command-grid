"""C2 — CharacterFittedShip: per-hull fitted-module state from ESI assets."""
from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone


class Migration(migrations.Migration):
    dependencies = [
        ("characters", "0002_initial"),
        ("sso", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="CharacterFittedShip",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source", models.CharField(choices=[("esi_char", "ESI (character token)"), ("esi_corp", "ESI (corporation/Director token)"), ("manual", "Manual entry"), ("zkill", "zKillboard"), ("everef", "EVE Ref"), ("sde", "Static Data Export"), ("estimated", "Estimated"), ("system", "System")], default="manual", max_length=16)),
                ("as_of", models.DateTimeField(default=timezone.now)),
                ("fetched_at", models.DateTimeField(blank=True, null=True)),
                ("item_id", models.BigIntegerField()),
                ("ship_type_id", models.IntegerField(db_index=True)),
                ("location_id", models.BigIntegerField(blank=True, null=True)),
                ("modules", models.JSONField(default=dict)),
                ("is_latest", models.BooleanField(default=True)),
                ("character", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="fitted_ships", to="sso.evecharacter")),
            ],
            options={"abstract": False},
        ),
        migrations.AddIndex(
            model_name="characterfittedship",
            index=models.Index(fields=["character", "is_latest"], name="characters__charact_b1bc9e_idx"),
        ),
    ]
