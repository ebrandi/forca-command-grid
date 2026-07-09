"""Seed a default SRP rule: full-fit basis, any doctrine, no cap, active.

This makes the program work out of the box; officers can refine or add
doctrine-specific rules later. Idempotent and reversible.
"""
from django.db import migrations


def create_default_rule(apps, schema_editor):
    SrpRule = apps.get_model("srp", "SrpRule")
    if not SrpRule.objects.filter(doctrine__isnull=True).exists():
        SrpRule.objects.create(doctrine=None, basis="fit", max_payout=0, active=True)


def remove_default_rule(apps, schema_editor):
    SrpRule = apps.get_model("srp", "SrpRule")
    SrpRule.objects.filter(doctrine__isnull=True, basis="fit", max_payout=0).delete()


class Migration(migrations.Migration):
    dependencies = [("srp", "0001_initial")]
    operations = [migrations.RunPython(create_default_rule, remove_default_rule)]
