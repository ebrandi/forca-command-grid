from django.db import migrations, models


class Migration(migrations.Migration):
    """Drop the never-recorded SEED/TRAIN/DELIVERY contribution kinds.

    Choices-only change (the column stays a CharField), so this is a no-op at the
    database level — it just keeps the migration state in sync with the model. No
    rows used those kinds (nothing ever wrote them).
    """

    dependencies = [
        ("pilots", "0002_alter_contributionevent_kind"),
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
                ],
                db_index=True,
                max_length=12,
            ),
        ),
    ]
