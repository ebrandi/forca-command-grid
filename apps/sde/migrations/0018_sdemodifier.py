"""Add SdeModifier — the normalised dogma modifierInfo graph (Tocha's Lab Phase 1).

Hand-written (the container cannot ``makemigrations`` against the mounted host dir). Additive:
a new table only, no changes to existing tables. Populated by ``manage.py import_dogma_graph``.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sde", "0017_sdetype_mass"),
    ]

    operations = [
        migrations.CreateModel(
            name="SdeModifier",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("effect_id", models.IntegerField()),
                ("func", models.CharField(max_length=64)),
                ("modified_attribute_id", models.IntegerField(blank=True, null=True)),
                ("modifying_attribute_id", models.IntegerField(blank=True, null=True)),
                ("operation", models.SmallIntegerField(blank=True, null=True)),
                ("group_id", models.IntegerField(blank=True, null=True)),
                ("skill_type_id", models.IntegerField(blank=True, null=True)),
                ("domain", models.CharField(blank=True, max_length=32)),
            ],
            options={
                "indexes": [
                    models.Index(fields=["effect_id"], name="sde_sdemodi_effect__26d796_idx"),
                    models.Index(fields=["modified_attribute_id"], name="sde_sdemodi_modifie_80bf1c_idx"),
                ],
            },
        ),
    ]
