from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("killboard", "0012_newbroconfig_pilotmilestone"),
    ]

    operations = [
        migrations.AddField(
            model_name="watchlist",
            name="alerts_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="watchlistentry",
            name="last_alerted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
