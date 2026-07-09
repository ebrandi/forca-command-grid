from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('killboard', '0010_killmail_role_value_index'),
    ]

    operations = [
        migrations.CreateModel(
            name='PilotRankNotification',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('character_id', models.BigIntegerField(unique=True)),
                ('character_name', models.CharField(blank=True, max_length=128)),
                ('last_notified_min_kills', models.PositiveIntegerField(default=0)),
                ('last_notified_rank_name', models.CharField(blank=True, max_length=64)),
                ('last_notified_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'abstract': False,
            },
        ),
    ]
