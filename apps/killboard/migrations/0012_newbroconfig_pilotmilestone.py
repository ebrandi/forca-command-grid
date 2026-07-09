from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('killboard', '0011_pilotranknotification'),
    ]

    operations = [
        migrations.CreateModel(
            name='NewbroConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('soften_danger_label', models.BooleanField(default=True, help_text="Show 'Learning' instead of 'Snuggly' for pilots below the activity floor.")),
                ('soften_below_events', models.PositiveIntegerField(default=20, help_text='Total kills + losses below which the danger label is softened.')),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='PilotMilestone',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('character_id', models.BigIntegerField(db_index=True)),
                ('character_name', models.CharField(blank=True, max_length=128)),
                ('kind', models.CharField(choices=[('first_kill', 'First kill'), ('first_solo', 'First solo kill'), ('first_final_blow', 'First final blow')], max_length=20)),
                ('achieved_at', models.DateTimeField()),
                ('killmail_id', models.BigIntegerField(blank=True, null=True)),
                ('notified_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'ordering': ['-achieved_at'],
                'constraints': [models.UniqueConstraint(fields=('character_id', 'kind'), name='uniq_pilot_milestone')],
            },
        ),
    ]
