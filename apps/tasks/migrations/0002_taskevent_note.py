from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='taskevent',
            name='note',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
    ]
