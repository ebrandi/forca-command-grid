from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('skills', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='IdleQueueNudge',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('character_id', models.BigIntegerField(unique=True)),
                ('notified_at', models.DateTimeField(blank=True, null=True)),
            ],
        ),
    ]
