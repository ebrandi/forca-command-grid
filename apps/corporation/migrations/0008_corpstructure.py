# Generated for structures monitoring (leadership gap #1).

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("corporation", "0007_moonextraction"),
    ]

    operations = [
        migrations.CreateModel(
            name="CorpStructure",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source", models.CharField(choices=[("esi_char", "ESI (character token)"), ("esi_corp", "ESI (corporation/Director token)"), ("manual", "Manual entry"), ("zkill", "zKillboard"), ("everef", "EVE Ref"), ("sde", "Static Data Export"), ("estimated", "Estimated"), ("system", "System")], default="manual", max_length=16)),
                ("as_of", models.DateTimeField(default=django.utils.timezone.now)),
                ("fetched_at", models.DateTimeField(blank=True, null=True)),
                ("structure_id", models.BigIntegerField(unique=True)),
                ("name", models.CharField(blank=True, max_length=200)),
                ("type_id", models.IntegerField(db_index=True)),
                ("type_name", models.CharField(blank=True, max_length=120)),
                ("system_id", models.BigIntegerField(blank=True, null=True)),
                ("system_name", models.CharField(blank=True, max_length=120)),
                ("state", models.CharField(blank=True, db_index=True, max_length=32)),
                ("fuel_expires", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("state_timer_start", models.DateTimeField(blank=True, null=True)),
                ("state_timer_end", models.DateTimeField(blank=True, null=True)),
                ("unanchors_at", models.DateTimeField(blank=True, null=True)),
                ("reinforce_hour", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("services", models.JSONField(blank=True, default=list)),
            ],
            options={
                "ordering": ["fuel_expires", "name"],
            },
        ),
    ]
