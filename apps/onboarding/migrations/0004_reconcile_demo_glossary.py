"""Reconcile onboarding glossary rows that the ``seed_demo`` command overwrote.

``seed_demo`` used ``update_or_create`` and was run against real environments,
replacing the canonical (catalogue-translated) definitions of ``Doctrine`` and
``ISK`` with short demo text, adding demo-only ``Highsec`` and ``Tackle`` rows, and
leaving those rows untranslatable (their stored text no longer matched a msgid).

This migration is create-safe and edit-safe: every write is guarded on the exact
demo / pre-reword text, so a leader-authored edit is never clobbered. It also
re-words ``Tidi`` off its bare ``%`` and ships ``Tackle`` as a first-class term.
Reverse is a no-op (we cannot tell a reconciled row from a hand-authored one).
"""
from __future__ import annotations

from django.db import migrations

# --- canonical definitions (must stay char-for-char identical to the catalogue msgids) ---
CANON_DOCTRINE = "A standard ship fitting published by leadership — the exact hull, modules, and ammo the corp wants you to fly so the whole fleet works together. Fly the doctrine fit as written; it's also what makes your loss SRP-eligible."
CANON_ISK = "EVE's currency (InterStellar Kredits). Everything — ships, ammo, services — is priced in ISK, usually quoted in millions ('m') or billions ('b'). Losing a ship really means losing its ISK cost, which is why cheap, replaceable ships are king."
OLD_TIDI = "Time dilation — when a massive battle overloads the server, EVE deliberately slows time in that system (down to 10% speed) so it can process everything. Everything moves in slow motion for everyone equally; it's normal, so stay calm and follow orders."
NEW_TIDI = "Time dilation — when a massive battle overloads the server, EVE deliberately slows time in that system, sometimes to a tenth of normal speed, so it can process everything. Everything moves in slow motion for everyone equally; it's normal, so stay calm and follow orders."
CANON_TACKLE = "Catching hostile ships so they can't warp away — a tackler pins a target in place with a warp scrambler or disruptor ('scram' or 'point') while the fleet kills it. It's one of the highest-impact jobs a newer pilot can fly: nothing dies if nothing is tackled."

# --- demo text seed_demo wrote (the only values we are allowed to overwrite) ---
DEMO_DOCTRINE = 'An official corp fit everyone is expected to be able to fly.'
DEMO_ISK = "EVE's currency."
DEMO_HIGHSEC = 'High-security space (0.5–1.0); CONCORD punishes aggression.'
DEMO_TACKLE = 'A ship/role that holds enemies in place (warp scram/disruptor).'


def reconcile(apps, schema_editor):
    Term = apps.get_model("onboarding", "GlossaryTerm")
    # Restore the canonical, translated definitions seed_demo clobbered.
    Term.objects.filter(term="Doctrine", definition=DEMO_DOCTRINE).update(definition=CANON_DOCTRINE)
    Term.objects.filter(term="ISK", definition=DEMO_ISK).update(definition=CANON_ISK)
    # Re-word Tidi off the bare "%" so it can enter the catalogue.
    Term.objects.filter(term="Tidi", definition=OLD_TIDI).update(definition=NEW_TIDI)
    # Drop the demo-only standalone "Highsec" (covered by "Highsec / Lowsec / Nullsec").
    Term.objects.filter(term="Highsec", definition=DEMO_HIGHSEC).delete()
    # Ship "Tackle" as a first-class term: create it where missing, upgrade the demo row.
    obj, created = Term.objects.get_or_create(term="Tackle", defaults={"definition": CANON_TACKLE})
    if not created and obj.definition == DEMO_TACKLE:
        obj.definition = CANON_TACKLE
        obj.save(update_fields=["definition"])


class Migration(migrations.Migration):
    dependencies = [
        ("onboarding", "0003_seed_milestones_glossary"),
    ]

    operations = [
        migrations.RunPython(reconcile, migrations.RunPython.noop),
    ]
