"""Phase 5 — ReadinessAlert + ExecutiveReport (hand-authored, additive, empty tables)."""
from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("readiness", "0004_pilot_readiness"),
    ]

    operations = [
        migrations.CreateModel(
            name="ReadinessAlert",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("rule_key", models.CharField(db_index=True, max_length=60)),
                ("dimension_key", models.CharField(blank=True, max_length=40)),
                ("kpi_key", models.CharField(blank=True, max_length=40)),
                ("severity", models.CharField(blank=True, max_length=8)),
                ("summary", models.CharField(max_length=300)),
                ("channels", models.JSONField(blank=True, default=list)),
                ("escalated_at", models.DateTimeField(blank=True, null=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("finding", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="+", to="readiness.readinessfinding")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="readinessalert",
            index=models.Index(fields=["rule_key", "resolved_at"], name="readiness_r_rule_ke_8951ed_idx"),
        ),
        migrations.CreateModel(
            name="ExecutiveReport",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("period_start", models.DateField(db_index=True)),
                ("period_end", models.DateField()),
                ("index", models.IntegerField(default=0)),
                ("body", models.JSONField(blank=True, default=dict)),
                ("delivered_channels", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["-period_start"]},
        ),
        migrations.AddConstraint(
            model_name="executivereport",
            constraint=models.UniqueConstraint(
                fields=("period_start", "period_end"), name="uniq_exec_report_period"),
        ),
    ]
