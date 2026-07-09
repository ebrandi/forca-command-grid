from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('planetary', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='picolony',
            name='alerted_sig',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
    ]
