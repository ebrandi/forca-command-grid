from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('corporation', '0010_structurealertconfig'),
    ]

    operations = [
        migrations.AddField(
            model_name='moonextraction',
            name='reminders_sent',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
