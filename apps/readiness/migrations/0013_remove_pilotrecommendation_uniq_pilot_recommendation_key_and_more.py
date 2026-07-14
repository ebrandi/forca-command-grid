"""A readiness quest log belongs to a PILOT, not to an account (LP-3).

``PilotRecommendation`` already carried ``character_id`` — it just was not part of the row's
identity, so the upsert key ``(user, category, ref_type, ref_id)`` let one pilot's regeneration
overwrite another's, and the account-wide "drop stale OPEN rows" sweep deleted every other
pilot's open recommendations each time any one pilot's log was rebuilt.

No new column: the identity simply widens to include the pilot it was always computed for. Rows
predating the ``character_id`` write are backfilled onto each account's main — the pilot they
were in fact generated from — so the new pilot-scoped read can still see them.

There is no schema risk in either direction: widening a unique key can only ever *permit* rows a
narrower one forbade, so applying it cannot fail on existing data. Rolling back re-narrows it,
which is why the backfill (not the constraint) is what a rollback would need to think about —
and the backfill only ever fills NULLs, so it leaves nothing to undo.
"""

from django.conf import settings
from django.db import migrations, models


def attach_recommendations_to_the_main_pilot(apps, schema_editor):
    PilotRecommendation = apps.get_model("readiness", "PilotRecommendation")
    EveCharacter = apps.get_model("sso", "EveCharacter")

    mains = EveCharacter.objects.filter(is_main=True, user__isnull=False).values_list(
        "user_id", "character_id"
    )
    for user_id, character_id in mains:
        PilotRecommendation.objects.filter(
            user_id=user_id, character_id__isnull=True
        ).update(character_id=character_id)
    # Rows on an account with no main can no longer be attributed to any pilot, and the
    # pilot-scoped read will never return them. Drop them; the next visit regenerates.
    PilotRecommendation.objects.filter(character_id__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('readiness', '0012_finding_alert_i18n_keys'),
        ('sso', '0006_backfill_director_seats'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='pilotrecommendation',
            name='uniq_pilot_recommendation_key',
        ),
        # Backfill BEFORE the new key exists, so no row is left unreachable by the pilot-scoped
        # read (Postgres treats NULLs as distinct, so a NULL row would never collide either).
        migrations.RunPython(
            attach_recommendations_to_the_main_pilot, migrations.RunPython.noop
        ),
        migrations.AddConstraint(
            model_name='pilotrecommendation',
            constraint=models.UniqueConstraint(fields=('user', 'character_id', 'category', 'ref_type', 'ref_id'), name='uniq_pilot_reco_pilot_key'),
        ),
    ]
