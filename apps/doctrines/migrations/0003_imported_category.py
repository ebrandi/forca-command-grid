"""Seed the IMPORTED category — the default home for fits imported from ESI.

Idempotent and reversible: the category is created if missing and removed on
reverse only when it still holds no doctrines.
"""
from django.db import migrations

IMPORTED_KEY = "imported"
IMPORTED_LABEL = "IMPORTED"


def create_imported(apps, schema_editor):
    DoctrineCategory = apps.get_model("doctrines", "DoctrineCategory")
    DoctrineCategory.objects.get_or_create(
        key=IMPORTED_KEY,
        defaults={"label": IMPORTED_LABEL, "sort_order": 100,
                  "description": "Ship fits imported from a director's saved EVE fittings."},
    )


def remove_imported(apps, schema_editor):
    DoctrineCategory = apps.get_model("doctrines", "DoctrineCategory")
    cat = DoctrineCategory.objects.filter(key=IMPORTED_KEY).first()
    if cat and not cat.doctrines.exists():
        cat.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("doctrines", "0002_initial"),
    ]

    operations = [
        migrations.RunPython(create_imported, remove_imported),
    ]
