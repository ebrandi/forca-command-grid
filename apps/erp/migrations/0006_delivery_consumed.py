from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('erp', '0005_buildjob_source_item'),
    ]

    operations = [
        migrations.AddField(
            model_name='delivery',
            name='consumed',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
