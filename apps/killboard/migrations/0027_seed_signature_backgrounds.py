"""WS-2 — seed the Combat Signatures background catalogue from the committed manifest.

Reuses the same ``sync_from_manifest`` upsert the ``sync_signature_backgrounds`` command uses, so
a fresh install gets every procedurally-generated background enabled from day one, while a re-run
never clobbers an admin's enable/disable choices (and retires, rather than deletes, any key that
has left the manifest). The manifest is resolved against ``settings.BASE_DIR`` (never the working
directory), so the migration is robust to where ``manage.py`` is invoked from.
"""
from __future__ import annotations

from django.db import migrations

from apps.killboard.signature_assets import load_manifest, sync_from_manifest


def seed_backgrounds(apps, schema_editor):
    SignatureBackground = apps.get_model("killboard", "SignatureBackground")
    sync_from_manifest(load_manifest(), SignatureBackground)


def unseed_backgrounds(apps, schema_editor):
    SignatureBackground = apps.get_model("killboard", "SignatureBackground")
    keys = [bg["key"] for bg in load_manifest().get("backgrounds", [])]
    SignatureBackground.objects.filter(key__in=keys).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("killboard", "0026_signaturebackground_signaturescanstate_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_backgrounds, unseed_backgrounds),
    ]
