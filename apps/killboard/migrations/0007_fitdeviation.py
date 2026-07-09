import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("doctrines", "0001_initial"),
        ("killboard", "0006_killboard_alliance_indexes"),
    ]

    operations = [
        migrations.CreateModel(
            name="FitDeviation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("missing", models.JSONField(default=list)),
                ("extra", models.JSONField(default=list)),
                (
                    "doctrine_fit",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="loss_deviations",
                        to="doctrines.doctrinefit",
                    ),
                ),
                (
                    "killmail",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="fit_deviation",
                        to="killboard.killmail",
                    ),
                ),
            ],
            options={"abstract": False},
        ),
    ]
