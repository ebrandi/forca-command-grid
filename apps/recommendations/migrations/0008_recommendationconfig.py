from django.db import migrations, models


def _seed(apps, schema_editor):
    M = apps.get_model("recommendations", "RecommendationConfig")
    M.objects.get_or_create(is_active=True)


class Migration(migrations.Migration):

    dependencies = [
        ('recommendations', '0007_alter_alert_channel'),
    ]

    operations = [
        migrations.CreateModel(
            name='RecommendationConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('is_active', models.BooleanField(default=True)),
                ('disabled_evaluators', models.JSONField(blank=True, default=list, help_text='Evaluator keys to skip (see the tuning console).')),
                ('combat_loss_window_days', models.PositiveSmallIntegerField(default=7)),
                ('combat_loss_threshold', models.PositiveSmallIntegerField(default=3)),
                ('min_severity', models.PositiveSmallIntegerField(default=0, help_text='Drop drafts scoring below this severity (0 = keep all).')),
            ],
            options={'abstract': False},
        ),
        migrations.RunPython(_seed, migrations.RunPython.noop),
    ]
