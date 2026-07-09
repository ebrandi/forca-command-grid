"""Phase 2 — per-pilot output tables (created now, populated in Phase 4)."""
from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("readiness", "0003_finding"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PilotReadinessSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("character_id", models.BigIntegerField(db_index=True)),
                ("overall", models.IntegerField(default=0)),
                ("facets", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("user", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="PilotRecommendation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("character_id", models.BigIntegerField(blank=True, null=True)),
                ("category", models.CharField(
                    choices=[("ship", "Ship"), ("skill", "Skill"), ("asset", "Asset"),
                             ("role", "Role"), ("industry", "Industry"), ("logistics", "Logistics")],
                    max_length=12)),
                ("title", models.CharField(max_length=200)),
                ("detail", models.TextField(blank=True)),
                ("priority", models.IntegerField(db_index=True, default=0)),
                ("points", models.IntegerField(default=0)),
                ("action_url", models.CharField(blank=True, max_length=300)),
                ("ref_type", models.CharField(blank=True, max_length=40)),
                ("ref_id", models.CharField(blank=True, max_length=64)),
                ("state", models.CharField(
                    choices=[("open", "Open"), ("done", "Done"), ("dismissed", "Dismissed")],
                    db_index=True, default="open", max_length=12)),
                ("snoozed_until", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="readiness_recommendations", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-priority", "-created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="pilotrecommendation",
            constraint=models.UniqueConstraint(
                fields=("user", "category", "ref_type", "ref_id"),
                name="uniq_pilot_recommendation_key",
            ),
        ),
    ]
