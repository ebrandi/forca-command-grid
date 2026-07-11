"""Seed the built-in career templates (doc 05 appendix, doc 15 migration plan).

Idempotent, reversible data migration: the forward step upserts every template from
``apps.capsuleer.templates_builtin`` by ``key`` (re-running is a no-op that refreshes built-ins
from code); the reverse step deletes only the built-in rows (by their known keys), leaving any
corp "clone-to-corp" templates untouched. Mirrors the campaigns ``0005_seed_builtin_templates``
pattern.
"""
from django.db import migrations


def seed(apps, schema_editor):
    from apps.capsuleer.templates_builtin import sync_builtin_templates

    CareerTemplate = apps.get_model("capsuleer", "CareerTemplate")
    sync_builtin_templates(model=CareerTemplate)


def unseed(apps, schema_editor):
    from apps.capsuleer.templates_builtin import BUILTIN_KEYS

    CareerTemplate = apps.get_model("capsuleer", "CareerTemplate")
    CareerTemplate.objects.filter(key__in=BUILTIN_KEYS, source="builtin").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("capsuleer", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
