"""Readiness index tuning (audit M2/M3).

- PilotReadinessSnapshot: replace the standalone character_id index with a
  (character_id, created_at) composite that serves both the per-pilot lookup and the
  ordered trend slice without a sort.
- ReadinessFinding: add a kpi_key index for the KPI drill-down (the unique constraint's
  leading column is dimension_key, so it did not serve a kpi_key filter).

Both tables are small (≤ a few thousand rows); index builds are sub-second.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("readiness", "0010_fleet_support_and_staging"),
    ]

    operations = [
        migrations.AlterField(
            model_name="pilotreadinesssnapshot",
            name="character_id",
            field=models.BigIntegerField(),
        ),
        migrations.AddIndex(
            model_name="pilotreadinesssnapshot",
            index=models.Index(
                fields=["character_id", "created_at"], name="readiness_p_charact_d79427_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="readinessfinding",
            index=models.Index(fields=["kpi_key"], name="readiness_r_kpi_key_4f1591_idx"),
        ),
    ]
