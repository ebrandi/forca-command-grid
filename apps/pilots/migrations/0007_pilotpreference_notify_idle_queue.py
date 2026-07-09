from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pilots', '0006_pvp_pve_weights'),
    ]

    operations = [
        migrations.AddField(
            model_name='pilotpreference',
            name='notify_idle_queue',
            field=models.BooleanField(default=False),
        ),
    ]
