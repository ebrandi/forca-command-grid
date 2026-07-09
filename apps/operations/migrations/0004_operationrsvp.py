# Generated for operation RSVP / availability.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("operations", "0003_structuretimer"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="OperationRsvp",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("character_name", models.CharField(blank=True, max_length=200)),
                ("response", models.CharField(choices=[("yes", "Coming"), ("maybe", "Maybe"), ("no", "Can't make it")], default="yes", max_length=5)),
                ("note", models.CharField(blank=True, max_length=200)),
                ("operation", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="rsvps", to="operations.operation")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="op_rsvps", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["response", "character_name"],
                "unique_together": {("operation", "user")},
            },
        ),
    ]
