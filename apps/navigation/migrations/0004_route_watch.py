from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("navigation", "0003_jumpplannerconfig_savedjumproute"),
    ]

    operations = [
        migrations.AddField(
            model_name="savedjumproute",
            name="watch_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="savedjumproute",
            name="alerted_sig",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]
