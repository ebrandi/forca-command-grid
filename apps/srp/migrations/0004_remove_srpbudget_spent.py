"""Dead-code sweep (roadmap 0.15): drop the never-written SrpBudget.spent column.

Spend is derived live from PAID claims by `services.spent_for_period`; the column
was never written by application code (always 0), so a stored value could only
drift out of sync. Confirmed on prod: sum(spent) == 0 across all rows.
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("srp", "0003_srp_program")]
    operations = [migrations.RemoveField(model_name="srpbudget", name="spent")]
