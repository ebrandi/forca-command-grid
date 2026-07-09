from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("erp", "0002_corp_industry"),
    ]

    operations = [
        migrations.AddField(
            model_name="buildjob",
            name="blocked_reason",
            field=models.CharField(
                blank=True,
                help_text="Why a queued job can't start (materials short).",
                max_length=200,
            ),
        ),
        migrations.AlterField(
            model_name="buildjob",
            name="status",
            field=models.CharField(
                choices=[
                    ("queued", "Queued"),
                    ("blocked", "Blocked"),
                    ("building", "Building"),
                    ("built", "Built"),
                    ("delivered", "Delivered"),
                    ("cancelled", "Cancelled"),
                ],
                db_index=True,
                default="queued",
                max_length=10,
            ),
        ),
    ]
