from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("recommendations", "0003_relayedmail"),
    ]

    operations = [
        migrations.AddField(
            model_name="recommendation",
            name="isk_impact",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=20),
        ),
    ]
