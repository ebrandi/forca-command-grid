# Seam B: persist a translatable scaffold key + params alongside the frozen English prose.
#
# Additive and metadata-only — no data migration, nothing is backfilled. A legacy row keeps an
# empty key and renders its stored English ``reason``/``detail`` verbatim (never blank).
#
# Every new column carries a **db_default**. Without it Django emits
#   ADD COLUMN … DEFAULT x NOT NULL   followed by   ALTER COLUMN … DROP DEFAULT
# which leaves a NOT NULL column with no database-level default: during a rollback, INSERTs from
# the older code (which does not know these columns) fail with a not-null violation, while reads
# keep working — so it looks healthy and breaks silently. This bit the production i18n deploy.
# ``db_default`` keeps the DEFAULT in the schema, so old code can still INSERT. Verified with
# ``manage.py sqlmigrate raffle 0007``: no DROP DEFAULT on these four columns.

from django.db import migrations, models
from django.db.models import Value


class Migration(migrations.Migration):

    dependencies = [
        ('raffle', '0006_raffleconfig_budget_warn_pct_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='raffleticketledgerentry',
            name='reason_key',
            field=models.CharField(blank=True, db_default='', default='', max_length=64),
        ),
        migrations.AddField(
            model_name='raffleticketledgerentry',
            name='reason_params',
            field=models.JSONField(
                blank=True, db_default=Value({}, models.JSONField()), default=dict
            ),
        ),
        migrations.AddField(
            model_name='rafflesuspiciousactivityflag',
            name='detail_key',
            field=models.CharField(blank=True, db_default='', default='', max_length=64),
        ),
        migrations.AddField(
            model_name='rafflesuspiciousactivityflag',
            name='detail_params',
            field=models.JSONField(
                blank=True, db_default=Value({}, models.JSONField()), default=dict
            ),
        ),
    ]
