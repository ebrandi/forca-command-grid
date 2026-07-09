from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sde', '0010_sde_name_trigram_indexes'),
    ]

    operations = [
        migrations.AddField(
            model_name='sdetype',
            name='primary_attribute_id',
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='sdetype',
            name='secondary_attribute_id',
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
    ]
