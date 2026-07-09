"""Drop redundant type_id indexes on MarketHistory / MarketPrice (audit R4).

Each table's unique_together — (type_id, region_id, date) and (type_id, location, profile)
— already has type_id as its leading column, so the standalone type_id index is redundant.
MarketHistory.region_id keeps its own index (2nd composite column, used by region scans).
Index-only migration; transparent to the ORM.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("market", "0002_markethistory"),
    ]

    operations = [
        migrations.AlterField(
            model_name="markethistory",
            name="type_id",
            field=models.IntegerField(),
        ),
        migrations.AlterField(
            model_name="marketprice",
            name="type_id",
            field=models.IntegerField(),
        ),
    ]
