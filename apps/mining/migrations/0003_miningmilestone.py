import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('mining', '0002_miningpayoutline_character_id_index'),
    ]

    operations = [
        migrations.CreateModel(
            name='MiningMilestone',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('threshold_m3', models.BigIntegerField()),
                ('reached_at', models.DateTimeField()),
                ('credited', models.BooleanField(default=False)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='mining_milestones', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['user', 'threshold_m3'],
                'unique_together': {('user', 'threshold_m3')},
            },
        ),
    ]
