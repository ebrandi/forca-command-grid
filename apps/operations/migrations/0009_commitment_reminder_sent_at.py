from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('operations', '0008_commitment_response'),
    ]

    operations = [
        migrations.AddField(
            model_name='operationcommitment',
            name='reminder_sent_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
