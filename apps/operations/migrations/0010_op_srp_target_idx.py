from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('operations', '0009_commitment_reminder_sent_at'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='operation',
            index=models.Index(fields=['srp', 'target_at'], name='op_srp_target_idx'),
        ),
    ]
