from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("operations", "0001_initial"),
        ("killboard", "0020_battlereport_sides"),
    ]

    operations = [
        migrations.CreateModel(
            name="CombatCampaign",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=200)),
                ("slug", models.CharField(blank=True, max_length=22, unique=True)),
                ("description", models.TextField(blank=True)),
                ("start_time", models.DateTimeField()),
                ("end_time", models.DateTimeField(blank=True, null=True)),
                ("visibility", models.CharField(
                    choices=[("member", "Members only"), ("public", "Public (shareable link)")],
                    default="member", max_length=8,
                )),
                ("scope", models.JSONField(blank=True, default=dict)),
                ("is_active", models.BooleanField(default=True)),
                ("srp_budget_isk", models.DecimalField(blank=True, decimal_places=2, max_digits=20, null=True)),
                ("doctrine_target_pct", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("created_by", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="+", to=settings.AUTH_USER_MODEL,
                )),
                ("operation", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="+", to="operations.operation",
                )),
            ],
            options={
                "ordering": ["-is_active", "-start_time", "-created_at"],
            },
        ),
    ]
