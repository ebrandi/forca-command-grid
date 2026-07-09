# Generated for ESI blueprint + industry-job import (Module O gap).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("erp", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="blueprint",
            name="item_id",
            field=models.BigIntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="blueprint",
            name="quantity",
            field=models.IntegerField(
                default=-1, help_text="ESI: -1 original (BPO), -2 copy (BPC)."
            ),
        ),
        migrations.AddField(
            model_name="blueprint",
            name="runs",
            field=models.IntegerField(
                default=-1, help_text="BPC runs remaining; -1 for an original."
            ),
        ),
        migrations.AddField(
            model_name="blueprint",
            name="location_id",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="CorpIndustryJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("job_id", models.BigIntegerField(unique=True)),
                ("installer_id", models.BigIntegerField(db_index=True)),
                ("activity_id", models.PositiveSmallIntegerField(default=1)),
                ("blueprint_type_id", models.IntegerField(db_index=True)),
                ("product_type_id", models.IntegerField(blank=True, db_index=True, null=True)),
                ("runs", models.IntegerField(default=1)),
                ("status", models.CharField(db_index=True, default="active", max_length=12)),
                ("facility_id", models.BigIntegerField(blank=True, null=True)),
                ("location_id", models.BigIntegerField(blank=True, null=True)),
                ("start_date", models.DateTimeField(blank=True, null=True)),
                ("end_date", models.DateTimeField(blank=True, db_index=True, null=True)),
            ],
            options={
                "ordering": ["end_date"],
            },
        ),
    ]
