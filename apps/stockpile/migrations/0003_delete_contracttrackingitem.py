"""Dead-code sweep (roadmap 0.15): drop the unused ContractTrackingItem model.

Nothing reads it — it was only ever written by the demo `seed_examples` command
and registered on the (prod-disabled) Django admin. No live feature depends on it.
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("stockpile", "0002_assetlocation_asset")]
    operations = [migrations.DeleteModel(name="ContractTrackingItem")]
