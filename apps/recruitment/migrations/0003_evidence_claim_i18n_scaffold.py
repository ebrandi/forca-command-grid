"""Seam B: persist a scaffold KEY + PARAMS beside the frozen English claim.

Additive and metadata-only — no row is read, written or backfilled. ``CandidateEvidence.claim``
is untouched: it stays the English fallback and the audit record of what a recruiter was actually
shown, and every legacy row keeps rendering from it because it carries no key (``claim_i18n``
falls back to the stored prose, so it can never blank).

Both new columns declare a **database-level** default (``db_default``), not merely a Django-level
one. Without it Django emits ``ADD COLUMN … DEFAULT x NOT NULL`` and then immediately
``ALTER COLUMN … DROP DEFAULT``, leaving a NOT NULL column with NO default in the schema. Reads
keep working, so it looks fine — but any INSERT issued by the *old* code during a rollback then
fails with a not-null violation. It breaks silently, and this exact trap bit the production i18n
deploy. ``db_default`` keeps the DEFAULT in the schema, so old and new code can both insert.

Verified with ``sqlmigrate recruitment 0003``: the DDL is two ``ADD COLUMN … DEFAULT … NOT NULL``
statements and NO ``DROP DEFAULT``.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("recruitment", "0002_evidence_claim_textfield"),
    ]

    operations = [
        migrations.AddField(
            model_name="candidateevidence",
            name="claim_key",
            field=models.CharField(blank=True, db_default="", default="", max_length=60),
        ),
        migrations.AddField(
            model_name="candidateevidence",
            name="claim_params",
            field=models.JSONField(
                blank=True,
                db_default=models.Value({}, models.JSONField()),
                default=dict,
            ),
        ),
    ]
