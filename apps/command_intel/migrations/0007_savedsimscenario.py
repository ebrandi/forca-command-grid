from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("command_intel", "0006_pilotdirective_completed_at"),
    ]

    operations = [
        migrations.CreateModel(
            name="SavedSimScenario",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=120)),
                ("scenario_key", models.CharField(max_length=40)),
                ("magnitude", models.FloatField(default=0)),
                ("notes", models.CharField(blank=True, max_length=280)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="saved_sim_scenarios", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["name", "id"],
            },
        ),
    ]
