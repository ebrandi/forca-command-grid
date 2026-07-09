from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('industry', '0004_delete_planetaryindustryplan'),
    ]

    operations = [
        migrations.AddField(
            model_name='industryeconomyconfig',
            name='consume_materials_on_delivery',
            field=models.BooleanField(default=False),
        ),
    ]
