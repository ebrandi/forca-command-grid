from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sso", "0003_authtoken_scopes_verified_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="evecharacter",
            name="director_checked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="evecharacter",
            index=models.Index(fields=["affiliation_updated_at"], name="evechar_affil_idx"),
        ),
        migrations.AddIndex(
            model_name="evecharacter",
            index=models.Index(fields=["director_checked_at"], name="evechar_director_idx"),
        ),
    ]
