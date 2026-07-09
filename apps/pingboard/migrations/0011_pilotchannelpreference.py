import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pingboard', '0010_fix_logistics_new_rule'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='PilotChannelPreference',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('kind', models.CharField(choices=[('slack', 'Slack'), ('telegram', 'Telegram'), ('whatsapp', 'WhatsApp'), ('discord', 'Discord')], max_length=16)),
                ('category', models.CharField(choices=[('emergency', 'Emergency'), ('home_defence', 'Home defence'), ('mining', 'Mining'), ('moon_extraction', 'Moon extraction'), ('pvp_fleet', 'PvP fleet'), ('roaming_gang', 'Roaming gang'), ('gatecamp', 'Gatecamp'), ('logistics', 'Logistics'), ('buyback', 'Buyback'), ('mentorship', 'Mentorship'), ('industry_job', 'Industry job'), ('structure_timer', 'Structure timer'), ('announcement', 'Corporation announcement'), ('system', 'System notification'), ('custom', 'Custom')], max_length=20)),
                ('muted', models.BooleanField(default=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='pingboard_prefs', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['kind', 'category'],
                'indexes': [models.Index(fields=['user', 'kind'], name='pingboard_p_user_id_374d9c_idx')],
                'constraints': [models.UniqueConstraint(fields=('user', 'kind', 'category'), name='pb_pref_user_kind_cat')],
            },
        ),
    ]
