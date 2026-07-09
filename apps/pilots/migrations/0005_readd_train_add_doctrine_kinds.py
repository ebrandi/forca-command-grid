from django.db import migrations, models


class Migration(migrations.Migration):
    """Re-add `train` (now with a real recorder) and add `doctrine` kind.

    Choices-only change — no DB schema impact.
    """

    dependencies = [
        ("pilots", "0004_contribution_points_weights"),
    ]

    operations = [
        migrations.AlterField(
            model_name="contributionevent",
            name="kind",
            field=models.CharField(
                choices=[
                    ("build", "Built"),
                    ("haul", "Hauled"),
                    ("task", "Completed task"),
                    ("srp", "Ship replacement"),
                    ("mining", "Mined"),
                    ("fleet", "Flew in fleet"),
                    ("train", "Trained skill"),
                    ("doctrine", "Unlocked doctrine"),
                ],
                db_index=True,
                max_length=12,
            ),
        ),
    ]
