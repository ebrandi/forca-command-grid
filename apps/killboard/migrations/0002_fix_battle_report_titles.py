"""Backfill battle-report titles that baked in a raw system id.

Earlier reports were titled ``Battle in system <id>`` (the literal id). Resolve
those to the system name where the SDE knows it; leave custom titles untouched.
"""
from __future__ import annotations

import re

from django.db import migrations

_DEFAULT_RE = re.compile(r"^Battle in system (\d+)$")


def fix_titles(apps, schema_editor):
    BattleReport = apps.get_model("killboard", "BattleReport")
    SdeSolarSystem = apps.get_model("sde", "SdeSolarSystem")
    for report in BattleReport.objects.all():
        m = _DEFAULT_RE.match(report.title or "")
        if not m:
            continue
        system_id = int(m.group(1))
        name = (
            SdeSolarSystem.objects.filter(system_id=system_id)
            .values_list("name", flat=True)
            .first()
        )
        if name:
            report.title = f"Battle in {name}"
            report.save(update_fields=["title"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("killboard", "0001_initial"),
        ("sde", "0001_initial"),
    ]

    operations = [migrations.RunPython(fix_titles, noop)]
