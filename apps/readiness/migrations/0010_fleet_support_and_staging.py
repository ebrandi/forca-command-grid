"""Gap B4/B5 — FleetSupportSkill + StagingSystem config tables.

Back the two new config-gated dimensions: a leadership-curated fleet-support skill
list (B4) and the corp staging system (B5). Both dimensions stay unavailable until
their table is populated.
"""
from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("readiness", "0009_doctrinereadinessconfig_is_upcoming"),
    ]

    operations = [
        migrations.CreateModel(
            name="FleetSupportSkill",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("skill_type_id", models.IntegerField(db_index=True)),
                ("skill_name", models.CharField(blank=True, max_length=120)),
                ("min_level", models.PositiveSmallIntegerField(default=5)),
                ("active", models.BooleanField(default=True)),
                ("sort_order", models.IntegerField(default=0)),
            ],
            options={
                "ordering": ["sort_order", "skill_name"],
            },
        ),
        migrations.AddConstraint(
            model_name="fleetsupportskill",
            constraint=models.UniqueConstraint(fields=("skill_type_id",), name="uniq_fleet_support_skill"),
        ),
        migrations.CreateModel(
            name="StagingSystem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("system_id", models.IntegerField(db_index=True)),
                ("system_name", models.CharField(blank=True, max_length=120)),
                ("active", models.BooleanField(default=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["-updated_at"],
            },
        ),
    ]
