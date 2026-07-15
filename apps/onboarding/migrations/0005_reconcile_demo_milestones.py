"""Reconcile onboarding MILESTONES that the ``seed_demo`` command clobbered.

Companion to 0004 (which fixed the glossary). ``seed_demo`` (update_or_create, run against
real environments) left ``link-character`` with a demo title and no description, ``import-skills``
with no description, and added a demo-only ``fly-newbro-tackle`` step. Those rows rendered their
stored (non-canonical) text verbatim in every locale — the canonical titles/descriptions are
already translated in the catalogue, so restoring them makes the milestones translate again.

Edit-safe: title/description are only restored while the row is still in the demo fingerprint
(demo title and/or empty description); a leader-authored value is left untouched. The demo-only
milestone is DEACTIVATED, never deleted — OnboardingProgress FKs it with on_delete=CASCADE, so a
delete would erase pilots' progress rows. Reverse is a no-op.
"""
from __future__ import annotations

from django.db import migrations

CANON = {
    "link-character": {'title': 'Link your EVE character', 'description': 'Log in with EVE SSO so the Grid knows who you are — and link every alt you fly, they all count.', 'category': 'account', 'criteria': {'type': 'linked'}, 'url': '', 'sort_order': 10},
    "import-skills": {'title': 'Import your skills', 'description': "Share your skill sheet so every 'can I fly this?' answer on this site is about YOU. One click — it keeps itself fresh afterwards.", 'category': 'skills', 'criteria': {'type': 'skills_imported'}, 'url': '/skills/', 'sort_order': 40},
}
DEMO_LC_TITLE = "Link your first character"
DEMO_TACKLE_KEY = "fly-newbro-tackle"
DEMO_TACKLE_TITLE = "Be able to fly the Newbro Tackle doctrine"


def reconcile(apps, schema_editor):
    M = apps.get_model("onboarding", "OnboardingMilestone")

    # link-character: demo overwrote the title AND left the description empty.
    m = M.objects.filter(key="link-character").first()
    if m and m.title == DEMO_LC_TITLE and not m.description:
        for f, v in CANON["link-character"].items():
            setattr(m, f, v)
        m.active = True
        m.save()

    # import-skills: demo kept the canonical title but blanked the description.
    m = M.objects.filter(key="import-skills").first()
    if m and not m.description:
        for f, v in CANON["import-skills"].items():
            setattr(m, f, v)
        m.active = True
        m.save()

    # fly-newbro-tackle: demo-only. Deactivate (hidden by the active=True view filter);
    # never delete — OnboardingProgress.on_delete=CASCADE would erase pilot progress.
    M.objects.filter(key=DEMO_TACKLE_KEY, title=DEMO_TACKLE_TITLE).update(active=False)


class Migration(migrations.Migration):
    dependencies = [
        ("onboarding", "0004_reconcile_demo_glossary"),
    ]

    operations = [
        migrations.RunPython(reconcile, migrations.RunPython.noop),
    ]
