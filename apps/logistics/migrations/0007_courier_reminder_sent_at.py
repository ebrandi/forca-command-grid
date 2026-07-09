from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('logistics', '0006_corpcontract'),
    ]

    operations = [
        migrations.AddField(
            model_name='couriercontract',
            name='reminder_sent_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
