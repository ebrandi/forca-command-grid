# Generated for the corp contracts oversight board (leadership gap #10).

from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("logistics", "0005_courier_verification"),
    ]

    operations = [
        migrations.CreateModel(
            name="CorpContract",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("contract_id", models.BigIntegerField(primary_key=True, serialize=False)),
                ("type", models.CharField(blank=True, max_length=20)),
                ("status", models.CharField(blank=True, db_index=True, max_length=24)),
                ("issuer_id", models.BigIntegerField(blank=True, null=True)),
                ("issuer_name", models.CharField(blank=True, max_length=200)),
                ("assignee_id", models.BigIntegerField(blank=True, null=True)),
                ("assignee_name", models.CharField(blank=True, max_length=200)),
                ("title", models.CharField(blank=True, max_length=255)),
                ("price", models.DecimalField(decimal_places=2, default=0, max_digits=20)),
                ("reward", models.DecimalField(decimal_places=2, default=0, max_digits=20)),
                ("volume", models.FloatField(default=0)),
                ("date_issued", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("date_expired", models.DateTimeField(blank=True, null=True)),
                ("date_completed", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "ordering": ["-date_issued"],
            },
        ),
    ]
