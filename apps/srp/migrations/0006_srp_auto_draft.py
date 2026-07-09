from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("srp", "0005_srp_fleet_op_gate"),
    ]

    operations = [
        migrations.AddField(
            model_name="srpprogram",
            name="auto_draft_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="srpprogram",
            name="auto_draft_since",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="srpclaim",
            name="auto_drafted",
            field=models.BooleanField(default=False),
        ),
    ]
