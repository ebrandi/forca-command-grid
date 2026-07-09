"""Phase 2 — the ReadinessFinding risk register (hand-authored, additive)."""
from __future__ import annotations

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("readiness", "0002_snapshot_kpis"),
        ("tasks", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ReadinessFinding",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("dimension_key", models.CharField(db_index=True, max_length=40)),
                ("kpi_key", models.CharField(blank=True, max_length=40)),
                ("severity", models.CharField(
                    choices=[("info", "Info"), ("warn", "Warning"), ("high", "High"), ("critical", "Critical")],
                    default="warn", max_length=8)),
                ("kind", models.CharField(
                    choices=[("gap", "Gap"), ("risk", "Risk"), ("forecast", "Forecast")],
                    default="gap", max_length=16)),
                ("title", models.CharField(max_length=200)),
                ("detail", models.TextField(blank=True)),
                ("weight", models.FloatField(default=0.0)),
                ("owner_tag", models.CharField(blank=True, max_length=40)),
                ("task_type", models.CharField(blank=True, max_length=12)),
                ("task_title", models.CharField(blank=True, max_length=200)),
                ("ref_type", models.CharField(blank=True, max_length=40)),
                ("ref_id", models.CharField(blank=True, max_length=64)),
                ("status", models.CharField(
                    choices=[("open", "Open"), ("acknowledged", "Acknowledged"), ("resolved", "Resolved")],
                    db_index=True, default="open", max_length=12)),
                ("predicted_breach_at", models.DateTimeField(blank=True, null=True)),
                ("first_seen", models.DateTimeField(default=django.utils.timezone.now)),
                ("last_seen", models.DateTimeField(default=django.utils.timezone.now)),
                ("task", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="+", to="tasks.task")),
            ],
            options={
                "ordering": ["-weight", "-last_seen"],
            },
        ),
        migrations.AddConstraint(
            model_name="readinessfinding",
            constraint=models.UniqueConstraint(
                fields=("dimension_key", "kpi_key", "ref_type", "ref_id"),
                name="uniq_readiness_finding_key",
            ),
        ),
        migrations.AddIndex(
            model_name="readinessfinding",
            index=models.Index(fields=["status", "dimension_key"], name="readiness_r_status_a30c15_idx"),
        ),
    ]
