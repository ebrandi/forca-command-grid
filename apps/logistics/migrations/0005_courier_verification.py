from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("logistics", "0004_jf_cyno_jump_pricing"),
    ]

    operations = [
        migrations.AddField(
            model_name="couriercontract",
            name="verification_state",
            field=models.CharField(
                choices=[
                    ("unverified", "Not verified"),
                    ("verified", "Verified in-game"),
                    ("failed", "Failed in-game"),
                ],
                db_index=True,
                default="unverified",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="couriercontract",
            name="verified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="couriercontract",
            name="esi_contract_id",
            field=models.BigIntegerField(blank=True, db_index=True, null=True),
        ),
    ]
