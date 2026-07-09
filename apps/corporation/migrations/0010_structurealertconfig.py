from django.db import migrations, models


def _seed(apps, schema_editor):
    """Seed the singleton so the read path (board flags / alert) never has to create it."""
    M = apps.get_model("corporation", "StructureAlertConfig")
    M.objects.get_or_create(is_active=True, defaults={"fuel_alert_days": 3, "adm_alert_floor": 3.0})


class Migration(migrations.Migration):

    dependencies = [
        ('corporation', '0009_friendlycorporation'),
    ]

    operations = [
        migrations.CreateModel(
            name='StructureAlertConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('is_active', models.BooleanField(default=True)),
                ('fuel_alert_days', models.PositiveSmallIntegerField(default=3, help_text='A structure with fewer than this many days of fuel is low and alerts.')),
                ('adm_alert_floor', models.FloatField(default=3.0, help_text='A sovereignty system with an ADM below this is soft and alerts.')),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.RunPython(_seed, migrations.RunPython.noop),
    ]
