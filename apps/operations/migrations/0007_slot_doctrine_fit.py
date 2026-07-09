import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("doctrines", "0001_initial"),
        ("operations", "0006_fleet_planner"),
    ]

    operations = [
        migrations.AddField(
            model_name="operationshipslot",
            name="doctrine_fit",
            field=models.ForeignKey(
                blank=True,
                help_text="The doctrine fit this slot is for, if it's an official doctrine ship.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="doctrines.doctrinefit",
            ),
        ),
        migrations.AddField(
            model_name="operationshipslot",
            name="eft_text",
            field=models.TextField(blank=True, help_text="EFT for a non-doctrine (custom) ship."),
        ),
    ]
