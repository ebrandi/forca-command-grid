"""Heal fitting revisions whose slots were stored as display labels, not rack tokens.

A doctrine loaded into Tocha's Lab before slot canonicalisation persisted its slots as the
doctrine library's display labels ("High 0", "Mid 1", "Drone Bay", "Cargo") rather than the
engine's rack tokens ("high", "med", ...). The editor's slot racks match on the token, so
those modules rendered in no rack at all (the hull looked empty), and the engine mis-slotted
them as low slots. This command rewrites each affected revision's items to canonical tokens
and expands racked modules one-per-slot (a single "High 0" x5 entry becomes five high-slot
entries), exactly as a fresh doctrine load now produces. Idempotent and safe to re-run.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.fitting.models import FitRevision
from apps.fitting.services import _RACKED_SLOTS, canonical_slot


def _heal_items(items: list[dict]) -> tuple[list[dict], bool]:
    """Return (canonicalised items, changed?). Only known labels are rewritten; an
    unrecognisable slot is left untouched so nothing is silently discarded."""
    out: list[dict] = []
    changed = False
    for it in items or []:
        raw = it.get("slot")
        canon = canonical_slot(raw)
        slot = canon if canon is not None else raw
        if canon is not None and canon != raw:
            changed = True
        qty = max(1, int(it.get("quantity", 1) or 1))
        if slot in _RACKED_SLOTS and qty > 1:
            changed = True
            for _ in range(qty):
                out.append({**it, "slot": slot, "quantity": 1})
        else:
            out.append({**it, "slot": slot})
    return out, changed


class Command(BaseCommand):
    help = "Canonicalise slot tokens on fitting revisions stored with display-label slots."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would change without writing.")

    def handle(self, *args, **options):
        dry = options["dry_run"]
        scanned = healed = 0
        with transaction.atomic():
            for rev in FitRevision.objects.all().iterator(chunk_size=500):
                scanned += 1
                new_items, changed = _heal_items(rev.items or [])
                if not changed:
                    continue
                healed += 1
                if not dry:
                    rev.items = new_items
                    rev.save(update_fields=["items"])
            if dry:
                transaction.set_rollback(True)
        verb = "would heal" if dry else "healed"
        self.stdout.write(self.style.SUCCESS(
            f"Scanned {scanned} revision(s); {verb} {healed}."))
