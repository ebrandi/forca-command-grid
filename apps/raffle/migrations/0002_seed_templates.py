"""Seed the built-in contest templates and a default raffle config.

Idempotent + reversible: re-running updates the built-in rows; reversing removes
the built-in templates only (leaving any leader-authored ones untouched).
"""
from __future__ import annotations

from django.db import migrations


def seed(apps, schema_editor):
    from apps.raffle.contest_templates import BUILTIN

    Template = apps.get_model("raffle", "RaffleContestTemplate")
    Config = apps.get_model("raffle", "RaffleConfig")
    for t in BUILTIN:
        Template.objects.update_or_create(
            key=t["key"],
            defaults={"name": t["name"], "description": t["description"],
                      "config": t["config"], "built_in": True, "active": True},
        )
    if not Config.objects.filter(is_active=True).exists():
        Config.objects.create(name="Default", is_active=True)


def unseed(apps, schema_editor):
    Template = apps.get_model("raffle", "RaffleContestTemplate")
    Template.objects.filter(built_in=True).delete()


class Migration(migrations.Migration):
    dependencies = [("raffle", "0001_initial")]
    operations = [migrations.RunPython(seed, unseed)]
