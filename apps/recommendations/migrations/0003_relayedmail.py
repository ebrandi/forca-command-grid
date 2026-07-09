# Generated for the EVE-mail relay (leadership gap #5).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("recommendations", "0002_corpnotification"),
    ]

    operations = [
        migrations.CreateModel(
            name="RelayedMail",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("mail_id", models.BigIntegerField(primary_key=True, serialize=False)),
                ("subject", models.CharField(blank=True, max_length=255)),
                ("from_id", models.BigIntegerField(blank=True, null=True)),
                ("from_name", models.CharField(blank=True, max_length=200)),
                ("sent_at", models.DateTimeField(db_index=True)),
            ],
            options={
                "ordering": ["-sent_at"],
            },
        ),
    ]
