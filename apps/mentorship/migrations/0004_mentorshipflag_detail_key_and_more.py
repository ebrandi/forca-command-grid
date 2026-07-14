# Seam B: the persisted mentorship prose gains a scaffold key + params alongside each English
# column, so a row written by a locale-less Celery worker can be re-rendered in the READER's
# locale (see ``apps/mentorship/messages.py``).
#
# Additive and metadata-only: no data migration, nothing is backfilled. A legacy row simply has no
# key and renders its stored English, never blank.
#
# ``db_default`` on every new column is load-bearing. Without it Django emits
# ``ADD COLUMN … DEFAULT x NOT NULL`` and then immediately ``ALTER COLUMN … DROP DEFAULT``, which
# leaves a NOT NULL column with NO database-level default: an INSERT issued by the *old* code
# during a rollback then dies with a not-null violation, while reads keep working — so it looks
# healthy and breaks silently. This exact trap bit the production i18n deploy. Verified with
# ``sqlmigrate``: there is no DROP DEFAULT on any column below.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("mentorship", "0003_mentorshiptaskvalidation_mentorship__result_9fbae5_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="mentorshipflag",
            name="detail_key",
            field=models.CharField(blank=True, db_default="", default="", max_length=64),
        ),
        migrations.AddField(
            model_name="mentorshipflag",
            name="detail_params",
            field=models.JSONField(
                blank=True,
                db_default=models.Value({}, models.JSONField()),
                default=dict,
            ),
        ),
        migrations.AddField(
            model_name="mentorshippairing",
            name="match_reasons_keys",
            field=models.JSONField(
                blank=True,
                db_default=models.Value([], models.JSONField()),
                default=list,
            ),
        ),
        migrations.AddField(
            model_name="mentorshippairingevent",
            name="detail_key",
            field=models.CharField(blank=True, db_default="", default="", max_length=64),
        ),
        migrations.AddField(
            model_name="mentorshippairingevent",
            name="detail_params",
            field=models.JSONField(
                blank=True,
                db_default=models.Value({}, models.JSONField()),
                default=dict,
            ),
        ),
        migrations.AddField(
            model_name="mentorshiptaskassignment",
            name="last_reason_key",
            field=models.CharField(blank=True, db_default="", default="", max_length=64),
        ),
        migrations.AddField(
            model_name="mentorshiptaskassignment",
            name="last_reason_params",
            field=models.JSONField(
                blank=True,
                db_default=models.Value({}, models.JSONField()),
                default=dict,
            ),
        ),
        migrations.AddField(
            model_name="mentorshiptaskvalidation",
            name="detail_key",
            field=models.CharField(blank=True, db_default="", default="", max_length=64),
        ),
        migrations.AddField(
            model_name="mentorshiptaskvalidation",
            name="detail_params",
            field=models.JSONField(
                blank=True,
                db_default=models.Value({}, models.JSONField()),
                default=dict,
            ),
        ),
    ]
