import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('corporation', '0008_corpstructure'),
    ]

    operations = [
        migrations.CreateModel(
            name='FriendlyCorporation',
            fields=[
                ('source', models.CharField(choices=[('esi_char', 'ESI (character token)'), ('esi_corp', 'ESI (corporation/Director token)'), ('manual', 'Manual entry'), ('zkill', 'zKillboard'), ('everef', 'EVE Ref'), ('sde', 'Static Data Export'), ('estimated', 'Estimated'), ('system', 'System')], default='manual', max_length=16)),
                ('as_of', models.DateTimeField(default=django.utils.timezone.now)),
                ('fetched_at', models.DateTimeField(blank=True, null=True)),
                ('corporation_id', models.BigIntegerField(primary_key=True, serialize=False)),
                ('name', models.CharField(blank=True, help_text='Optional label; the id is what grants access.', max_length=200)),
                ('note', models.CharField(blank=True, max_length=200)),
                ('active', models.BooleanField(default=True)),
            ],
            options={
                'verbose_name': 'friendly corporation',
                'verbose_name_plural': 'friendly corporations',
                'ordering': ['name', 'corporation_id'],
            },
        ),
    ]
