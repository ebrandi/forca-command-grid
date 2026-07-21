from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('killboard', '0016_killfeedconfig_exclude_awox_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='KillboardApiToken',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(blank=True, help_text='A label to tell this token apart from your others (e.g. “Discord bot”).', max_length=100)),
                ('key_hash', models.CharField(db_index=True, max_length=64, unique=True)),
                ('prefix', models.CharField(blank=True, max_length=12)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('last_used_at', models.DateTimeField(blank=True, null=True)),
                ('revoked_at', models.DateTimeField(blank=True, null=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='killboard_api_tokens', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['user', '-created_at'], name='kbtok_user_created_idx')],
            },
        ),
    ]
