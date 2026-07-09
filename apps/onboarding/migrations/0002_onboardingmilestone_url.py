from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("onboarding", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="onboardingmilestone",
            name="url",
            field=models.CharField(blank=True, default="", max_length=300),
        ),
    ]
