# Linked Pilots (LP-1/LP-4): per-pilot link metadata and per-pilot Director status.
#
# ``db_default`` is load-bearing on the two NOT NULL columns. A bare ``default=`` is enforced
# in Python only: Django emits ``ADD COLUMN … DEFAULT x NOT NULL`` and then immediately
# ``ALTER COLUMN … DROP DEFAULT``, leaving a NOT NULL column with no database default. Roll the
# code back without rolling back the schema and every INSERT from the old code dies on a
# not-null violation while reads and /healthz stay green. ``manage.py rollback_safety`` (run by
# scripts/rollback.sh) inspects the live database for exactly this shape and refuses the
# rollback; with ``db_default`` these columns pass.
#
# ``last_used_at`` is nullable, which is safe by construction: old code simply never writes it.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sso', '0004_evecharacter_director_checked_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='evecharacter',
            name='display_order',
            field=models.SmallIntegerField(db_default=0, default=0),
        ),
        migrations.AddField(
            model_name='evecharacter',
            name='is_corp_director',
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.AddField(
            model_name='evecharacter',
            name='last_used_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
