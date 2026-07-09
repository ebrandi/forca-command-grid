from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sso", "0002_evecharacter_owner_hash"),
    ]

    operations = [
        migrations.AddField(
            model_name="authtoken",
            name="scopes_verified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
