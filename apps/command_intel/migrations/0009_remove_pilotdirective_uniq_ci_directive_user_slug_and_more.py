"""Scope a pilot's quest log to the PILOT, not to the account (LP-3).

``PilotDirective`` was upserted and read by ``(user, slug)`` while being *computed* from exactly
one character. The instant an account can fly more than one pilot, regenerating the quest log for
pilot B overwrites pilot A's rows and the survivor is shown to whoever asks — pilot-level data
merged in the database, which no cache key can fix, because the row only exists once.

The backfill is the load-bearing part, and it runs BETWEEN the AddField and the AddConstraint.
Existing rows were generated from each account's MAIN (both the warmer and the dashboard resolved
``is_main``), so that is the pilot they belong to. Left on NULL they would be invisible to the new
pilot-scoped read — which filters on a character id — while still not colliding with anything,
because Postgres treats NULLs as distinct in a unique index. They would simply linger as
unreachable ghosts.

The column is nullable, so the rollback story is clean: old code never writes it, and
``manage.py rollback_safety`` (run by scripts/rollback.sh) passes.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def attach_directives_to_the_main_pilot(apps, schema_editor):
    PilotDirective = apps.get_model("command_intel", "PilotDirective")
    EveCharacter = apps.get_model("sso", "EveCharacter")

    mains = EveCharacter.objects.filter(is_main=True, user__isnull=False).values_list(
        "user_id", "character_id"
    )
    for user_id, character_id in mains:
        PilotDirective.objects.filter(user_id=user_id, character__isnull=True).update(
            character_id=character_id
        )
    # An account with no main at all cannot have its directives attributed to any pilot; they
    # became unreachable the moment the read went pilot-scoped. Drop them rather than leave dead
    # rows behind — the next dashboard visit regenerates the pilot's quest log from scratch.
    PilotDirective.objects.filter(character__isnull=True).delete()


def detach_directives(apps, schema_editor):
    PilotDirective = apps.get_model("command_intel", "PilotDirective")
    PilotDirective.objects.update(character=None)


class Migration(migrations.Migration):

    dependencies = [
        ('command_intel', '0008_seam_b_scaffold_keys'),
        ('sso', '0006_backfill_director_seats'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='pilotdirective',
            name='uniq_ci_directive_user_slug',
        ),
        migrations.AddField(
            model_name='pilotdirective',
            name='character',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='command_intel_directives', to='sso.evecharacter'),
        ),
        # Between the column and the constraint — see the module docstring.
        migrations.RunPython(attach_directives_to_the_main_pilot, detach_directives),
        migrations.AddIndex(
            model_name='pilotdirective',
            index=models.Index(fields=['character', 'state'], name='command_int_charact_ca7bf9_idx'),
        ),
        migrations.AddConstraint(
            model_name='pilotdirective',
            constraint=models.UniqueConstraint(fields=('user', 'character', 'slug'), name='uniq_ci_directive_pilot_slug'),
        ),
    ]
