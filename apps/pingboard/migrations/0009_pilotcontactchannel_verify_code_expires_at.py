# Adds a TTL to a pilot's channel-verification code so a leaked/stale code
# cannot be redeemed indefinitely to bind a chat id to their pilot.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pingboard', '0008_seed_automation_rules'),
    ]

    operations = [
        migrations.AddField(
            model_name='pilotcontactchannel',
            name='verify_code_expires_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
