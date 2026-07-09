from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("sde", "0011_sdetype_skill_attributes"),
    ]

    operations = [
        migrations.AddField(
            model_name="sdetype",
            name="portion_size",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.CreateModel(
            name="SdeTypeMaterial",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("material_type_id", models.IntegerField(db_index=True)),
                ("quantity", models.BigIntegerField()),
                ("type", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                           related_name="reprocess_materials", to="sde.sdetype")),
            ],
        ),
        migrations.AddConstraint(
            model_name="sdetypematerial",
            constraint=models.UniqueConstraint(fields=("type", "material_type_id"), name="uniq_type_material"),
        ),
    ]
