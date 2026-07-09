"""C1 — SdeBlueprintSkill: manufacturing-skill requirements per blueprint product."""
from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sde", "0007_sdecelestial_sdetype_packaged_volume"),
    ]

    operations = [
        migrations.CreateModel(
            name="SdeBlueprintSkill",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("blueprint_type_id", models.IntegerField(db_index=True)),
                ("level", models.PositiveSmallIntegerField(default=1)),
                ("activity", models.CharField(default="manufacturing", max_length=32)),
                ("product_type", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="sde.sdetype")),
                ("skill_type", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="sde.sdetype")),
            ],
        ),
        migrations.AddIndex(
            model_name="sdeblueprintskill",
            index=models.Index(fields=["product_type", "activity"], name="sde_sdeblue_product_bfa776_idx"),
        ),
        migrations.AddConstraint(
            model_name="sdeblueprintskill",
            constraint=models.UniqueConstraint(
                fields=("product_type", "skill_type", "activity"), name="uniq_blueprint_skill"
            ),
        ),
    ]
