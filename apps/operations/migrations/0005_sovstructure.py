# Generated for sov ADM / iHub tracking (leadership gap #12).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("operations", "0004_operationrsvp"),
    ]

    operations = [
        migrations.CreateModel(
            name="SovStructure",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("structure_id", models.BigIntegerField(primary_key=True, serialize=False)),
                ("alliance_id", models.BigIntegerField(db_index=True)),
                ("solar_system_id", models.IntegerField(db_index=True)),
                ("system_name", models.CharField(blank=True, max_length=120)),
                ("structure_type_id", models.IntegerField(default=0)),
                ("adm", models.FloatField(default=1.0, help_text="Activity Defense Multiplier (1.0–6.0).")),
                ("vulnerable_start", models.DateTimeField(blank=True, null=True)),
                ("vulnerable_end", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "ordering": ["adm", "system_name"],
            },
        ),
    ]
