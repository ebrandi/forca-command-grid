"""Reprice jump freighters on cyno jumps instead of gate-derived low/null systems.

Renames the per-system field to per-jump (preserving the configured value) and
adds the assumed Jump Drive Calibration level that sets the jump range.
"""
from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("logistics", "0003_couriercontract_dest_location_id_and_more"),
    ]

    operations = [
        migrations.RenameField(
            model_name="ratecard",
            old_name="jf_per_lowsec_system",
            new_name="jf_per_jump",
        ),
        migrations.AddField(
            model_name="ratecard",
            name="jf_assumed_jdc",
            field=models.PositiveSmallIntegerField(default=5),
        ),
    ]
