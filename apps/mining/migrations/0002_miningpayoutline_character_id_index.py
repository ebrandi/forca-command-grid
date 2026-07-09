from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mining', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='miningpayoutline',
            name='character_id',
            field=models.BigIntegerField(db_index=True),
        ),
    ]
