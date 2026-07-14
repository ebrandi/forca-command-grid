"""Seam B: persist a scaffold KEY + PARAMS beside the frozen English prose.

Additive and metadata-only — no row is read, written or backfilled. The existing prose columns
(``ReadinessFinding.title``/``detail``/``task_title``, ``ReadinessAlert.summary``) are untouched:
they stay the English fallback and the audit record, and every legacy row keeps rendering from
them because it carries no key.

Every new column declares a **database-level** default (``db_default``), not just a Django-level
one. Without it Django emits ``ADD COLUMN … DEFAULT x NOT NULL`` followed by
``ALTER COLUMN … DROP DEFAULT``, which leaves a NOT NULL column with no DB default — so any
INSERT issued by the *old* code during a rollback fails with a not-null violation while reads
keep working, i.e. it breaks silently. ``db_default`` keeps the DEFAULT in the schema, so old and
new code can both insert. (Verified: ``sqlmigrate`` emits no ``DROP DEFAULT`` for these columns.)
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("readiness", "0011_alter_pilotreadinesssnapshot_character_id_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="readinessfinding",
            name="title_key",
            field=models.CharField(blank=True, db_default="", default="", max_length=60),
        ),
        migrations.AddField(
            model_name="readinessfinding",
            name="title_params",
            field=models.JSONField(
                blank=True,
                db_default=models.Value({}, models.JSONField()),
                default=dict,
            ),
        ),
        migrations.AddField(
            model_name="readinessfinding",
            name="detail_key",
            field=models.CharField(blank=True, db_default="", default="", max_length=60),
        ),
        migrations.AddField(
            model_name="readinessfinding",
            name="detail_params",
            field=models.JSONField(
                blank=True,
                db_default=models.Value({}, models.JSONField()),
                default=dict,
            ),
        ),
        migrations.AddField(
            model_name="readinessfinding",
            name="task_title_key",
            field=models.CharField(blank=True, db_default="", default="", max_length=60),
        ),
        migrations.AddField(
            model_name="readinessfinding",
            name="task_title_params",
            field=models.JSONField(
                blank=True,
                db_default=models.Value({}, models.JSONField()),
                default=dict,
            ),
        ),
        migrations.AddField(
            model_name="readinessalert",
            name="summary_key",
            field=models.CharField(blank=True, db_default="", default="", max_length=60),
        ),
        migrations.AddField(
            model_name="readinessalert",
            name="summary_params",
            field=models.JSONField(
                blank=True,
                db_default=models.Value({}, models.JSONField()),
                default=dict,
            ),
        ),
    ]
