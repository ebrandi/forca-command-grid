"""Combat Signatures — sync the DB background rows with the committed manifest (WS-2).

Upserts one ``SignatureBackground`` row per manifest entry (name/category/display_order/version/
checksum), creating new designs enabled and preserving an admin's ``enabled`` choice on existing
ones. It NEVER deletes a row: a key that has been dropped from the manifest is *retired*
(``enabled=False``) so it stops being offered while any signature still referencing it keeps
working (the FK is ``PROTECT``). The seed data migration (0027) runs the very same
``sync_from_manifest`` helper, so this command is just the on-demand, re-runnable form used after
regenerating the library.

    manage.py sync_signature_backgrounds
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.killboard.models import SignatureBackground
from apps.killboard.signature_assets import load_manifest, manifest_path, sync_from_manifest


class Command(BaseCommand):
    help = "Upsert SignatureBackground rows from the committed background manifest (never deletes)."

    def handle(self, *args, **options) -> None:
        path = manifest_path()
        if not path.exists():
            raise CommandError(
                f"manifest not found at {path} — run generate_signature_backgrounds first."
            )
        manifest = load_manifest(path)
        created, updated, retired = sync_from_manifest(manifest, SignatureBackground)
        self.stdout.write(self.style.SUCCESS(
            f"Synced backgrounds: {created} created, {updated} updated, {retired} retired."
        ))
