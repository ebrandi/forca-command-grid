import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('operations', '0010_op_srp_target_idx'),
    ]

    operations = [
        migrations.CreateModel(
            name='OperationTemplate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=200)),
                ('type', models.CharField(choices=[('pvp', 'PvP fleet'), ('roam', 'Roaming gang'), ('gatecamp', 'Gate camp'), ('ratting', 'Ratting fleet'), ('mining', 'Mining operation'), ('logistics', 'Transport / logistics'), ('deployment', 'Deployment'), ('war_prep', 'War preparation'), ('home_defence', 'Home defence'), ('structure_timer', 'Structure timer'), ('doctrine_rollout', 'Doctrine rollout'), ('industrial', 'Industrial campaign')], default='pvp', max_length=20)),
                ('duration_minutes', models.PositiveIntegerField(default=60)),
                ('formup', models.CharField(blank=True, max_length=200)),
                ('destination', models.CharField(blank=True, max_length=200)),
                ('comms', models.CharField(blank=True, max_length=200)),
                ('link', models.CharField(blank=True, max_length=500)),
                ('notes', models.TextField(blank=True)),
                ('min_pilots', models.PositiveIntegerField(default=0)),
                ('srp', models.CharField(blank=True, choices=[('alliance', 'Alliance SRP'), ('corp', 'Corp SRP'), ('organiser', 'Organiser-funded SRP'), ('none', 'No SRP coverage')], max_length=10)),
                ('rsvp_offset_minutes', models.PositiveIntegerField(default=0, help_text='Instance RSVP deadline = this many minutes before form-up (0 = none).')),
                ('weekday', models.PositiveSmallIntegerField(default=5, help_text='0 = Monday … 6 = Sunday (UTC).')),
                ('hour', models.PositiveSmallIntegerField(default=20, help_text='Form-up hour, 0–23 (UTC).')),
                ('minute', models.PositiveSmallIntegerField(default=0, help_text='Form-up minute, 0–59 (UTC).')),
                ('lead_days', models.PositiveSmallIntegerField(default=10, help_text='Materialise instances this many days ahead.')),
                ('active', models.BooleanField(default=True)),
                ('fc', models.ForeignKey(blank=True, help_text='Default fleet commander for spawned ops.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['-active', 'name']},
        ),
        migrations.AddField(
            model_name='operation',
            name='recurring_template',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='instances', to='operations.operationtemplate'),
        ),
        migrations.CreateModel(
            name='OperationTemplateSlot',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('ship_name', models.CharField(max_length=200)),
                ('ship_type_id', models.BigIntegerField(blank=True, null=True)),
                ('role', models.CharField(choices=[('dps', 'DPS'), ('logi', 'Logistics'), ('tackle', 'Tackle'), ('scout', 'Scout'), ('booster', 'Booster'), ('hauler', 'Hauler'), ('miner', 'Miner'), ('command', 'Command ship'), ('ewar', 'EWAR'), ('other', 'Other')], default='dps', max_length=10)),
                ('min_pilots', models.PositiveIntegerField(default=1)),
                ('max_pilots', models.PositiveIntegerField(default=0, help_text='0 = no cap.')),
                ('priority', models.PositiveIntegerField(default=1)),
                ('template', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='slots', to='operations.operationtemplate')),
            ],
            options={'ordering': ['priority', 'id']},
        ),
    ]
