"""Seam B: persist a scaffold key + params beside the engine's English prose.

Additive and metadata-only — four AddFields, no data migration, nothing backfilled. The existing
``message`` / ``logic_summary`` columns are KEPT: they stay the canonical English, the audit record
and the fallback, so every row written before this migration simply has no key and renders its
stored English verbatim (never blank).

Every new column carries a **``db_default``**. Without one Django emits
``ADD COLUMN … DEFAULT x NOT NULL`` followed by ``ALTER COLUMN … DROP DEFAULT``, leaving a NOT NULL
column with no database-level default: reads keep working, so it looks fine, but any INSERT from
*older* code during a rollback fails with a not-null violation. ``db_default`` keeps the DEFAULT in
the DDL — verified with ``sqlmigrate``: there is no DROP DEFAULT on these four columns.
"""
from django.db import migrations, models
from django.db.models import Value


class Migration(migrations.Migration):

    dependencies = [
        ("recommendations", "0008_recommendationconfig"),
    ]

    operations = [
        migrations.AddField(
            model_name="recommendation",
            name="message_key",
            field=models.CharField(blank=True, db_default="", default="", max_length=64),
        ),
        migrations.AddField(
            model_name="recommendation",
            name="message_params",
            field=models.JSONField(
                blank=True, db_default=Value({}, models.JSONField()), default=dict
            ),
        ),
        migrations.AddField(
            model_name="recommendation",
            name="logic_summary_key",
            field=models.CharField(blank=True, db_default="", default="", max_length=64),
        ),
        migrations.AddField(
            model_name="recommendation",
            name="logic_summary_params",
            field=models.JSONField(
                blank=True, db_default=Value({}, models.JSONField()), default=dict
            ),
        ),
    ]
