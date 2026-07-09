"""Retire the legacy NotificationChannel Discord registry.

Discord (and every other chat channel) is now managed through Pingboard's
ChannelProvider; recommendations.notify.broadcast_discord / dispatch_alerts route
through pingboard.services.broadcast_text and no longer read this table.

The dependency on pingboard 0002 is a HARD ordering constraint, not incidental:
pingboard 0002 (import_notification_channels) copies NotificationChannel rows into
ChannelProvider inside a RunPython, so on a fresh database that copy migration MUST
run before this DeleteModel drops the table — otherwise 0002 queries a table that no
longer exists and the migration graph fails. (On prod 0002 is long applied, so this
just drops a now-dead table.) Do not "simplify" this dependency away.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("recommendations", "0005_retire_member_recs"),
        ("pingboard", "0002_import_notification_channels"),
    ]

    operations = [
        migrations.DeleteModel(name="NotificationChannel"),
    ]
