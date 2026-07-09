"""Gap B — DoctrineReadinessConfig.is_upcoming (drives doctrine.upcoming_coverage)."""
from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("readiness", "0008_finding_score"),
    ]

    operations = [
        migrations.AddField(
            model_name="doctrinereadinessconfig",
            name="is_upcoming",
            field=models.BooleanField(default=False),
        ),
    ]
