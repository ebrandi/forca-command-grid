"""Phase 6 — MandatoryShip + StrategicRoleTarget config tables (hand-authored, additive)."""
from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("readiness", "0005_alerts_reports"),
        ("doctrines", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="MandatoryShip",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("label", models.CharField(max_length=120)),
                ("category", models.CharField(
                    choices=[("travel", "Travel"), ("doctrine", "Doctrine"),
                             ("home_defense", "Home defense"), ("cyno", "Cyno"),
                             ("scout", "Scout"), ("other", "Other")],
                    default="other", max_length=20)),
                ("ship_type_id", models.BigIntegerField(blank=True, null=True)),
                ("required_quantity", models.PositiveIntegerField(default=1)),
                ("required_location_kind", models.CharField(
                    choices=[("any", "Anywhere"), ("system", "Specific system"),
                             ("structure", "Specific structure")],
                    default="any", max_length=10)),
                ("required_system_id", models.BigIntegerField(blank=True, null=True)),
                ("required_structure_id", models.BigIntegerField(blank=True, null=True)),
                ("require_fitted", models.BooleanField(default=False)),
                ("required_clone", models.BooleanField(default=False)),
                ("required_implants", models.JSONField(blank=True, default=list)),
                ("applies_to_role", models.CharField(blank=True, max_length=20)),
                ("active", models.BooleanField(default=True)),
                ("sort_order", models.IntegerField(default=0)),
                ("doctrine_fit", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="+", to="doctrines.doctrinefit")),
            ],
            options={"ordering": ["sort_order", "label"]},
        ),
        migrations.CreateModel(
            name="StrategicRoleTarget",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("role_key", models.CharField(max_length=20, unique=True)),
                ("label", models.CharField(max_length=80)),
                ("desired_count", models.PositiveIntegerField(default=0)),
                ("detection", models.CharField(
                    choices=[("skills", "By skills"), ("asset", "By asset"), ("manual", "Manual")],
                    default="manual", max_length=12)),
                ("detection_params", models.JSONField(blank=True, default=dict)),
                ("active", models.BooleanField(default=True)),
            ],
            options={"ordering": ["role_key"]},
        ),
    ]
