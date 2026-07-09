"""Dead-code sweep (roadmap 0.15): drop the unused Alert.Channel.DISCORD choice.

Multi-channel delivery moved to Pingboard; Alert rows are always written IN_APP
(`notify.py`). No prod Alert row uses the "discord" channel. Choices-only change.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("recommendations", "0006_delete_notificationchannel")]
    operations = [
        migrations.AlterField(
            model_name="alert",
            name="channel",
            field=models.CharField(
                choices=[("in_app", "In-app")], default="in_app", max_length=10
            ),
        ),
    ]
