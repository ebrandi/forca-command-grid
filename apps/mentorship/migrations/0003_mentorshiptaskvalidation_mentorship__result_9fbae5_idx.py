"""Add (result, source) index to MentorshipTaskValidation (audit M1).

Officer reporting group-bys scan this append-only validation log filtering result=FAIL
and source=MENTOR/result=PASS; the composite serves both. Append-only ⇒ no update churn.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("mentorship", "0002_seed_mentorship"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="mentorshiptaskvalidation",
            index=models.Index(
                fields=["result", "source"], name="mentorship__result_9fbae5_idx"
            ),
        ),
    ]
