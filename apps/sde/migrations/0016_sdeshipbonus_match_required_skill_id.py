from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sde", "0015_sdedogmaattribute_sdedogmaeffect_sdeshipbonus_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="sdeshipbonus",
            name="match_required_skill_id",
            field=models.IntegerField(blank=True, null=True),
        ),
    ]
