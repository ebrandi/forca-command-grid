from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("operations", "0007_slot_doctrine_fit"),
    ]

    operations = [
        migrations.AddField(
            model_name="operationcommitment",
            name="response",
            field=models.CharField(
                choices=[("yes", "Coming"), ("maybe", "Maybe")],
                default="yes",
                max_length=5,
            ),
        ),
    ]
