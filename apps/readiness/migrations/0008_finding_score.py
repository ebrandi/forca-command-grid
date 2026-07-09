"""Gap E — ReadinessFinding.score (hand-authored, additive) for score-precise alerts."""
from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("readiness", "0007_doctrine_readiness_config"),
    ]

    operations = [
        migrations.AddField(
            model_name="readinessfinding",
            name="score",
            field=models.IntegerField(blank=True, null=True),
        ),
    ]
