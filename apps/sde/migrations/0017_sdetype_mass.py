from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sde", "0016_sdeshipbonus_match_required_skill_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="sdetype",
            name="mass",
            field=models.FloatField(blank=True, null=True),
        ),
    ]
