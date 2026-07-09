import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ImpersonationSession',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('actor_label', models.CharField(blank=True, max_length=200)),
                ('target_label', models.CharField(blank=True, max_length=200)),
                ('reason', models.CharField(blank=True, max_length=200)),
                ('ip', models.GenericIPAddressField(blank=True, null=True)),
                ('started_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('ended_at', models.DateTimeField(blank=True, null=True)),
                ('end_reason', models.CharField(blank=True, choices=[('', 'Active'), ('manual', 'Exited by director'), ('expired', 'Auto-expired (max duration)'), ('actor_invalid', 'Director no longer authorised'), ('target_invalid', 'Target no longer impersonatable'), ('logout', 'Director logged out')], default='', max_length=16)),
                ('actor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='impersonations_made', to=settings.AUTH_USER_MODEL)),
                ('target', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='impersonated_sessions', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-started_at'],
                'indexes': [models.Index(fields=['ended_at', 'started_at'], name='impersonati_ended_a_b27484_idx')],
            },
        ),
    ]
