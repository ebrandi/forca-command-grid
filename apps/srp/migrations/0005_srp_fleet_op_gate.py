from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('srp', '0004_remove_srpbudget_spent'),
    ]

    operations = [
        migrations.AddField(
            model_name='srpprogram',
            name='require_fleet_op',
            field=models.BooleanField(default=False, help_text="Only cover losses during a sanctioned fleet op's window."),
        ),
        migrations.AddField(
            model_name='srpprogram',
            name='fleet_op_grace_minutes',
            field=models.PositiveIntegerField(default=30, help_text='Minutes of grace added before and after an op window (form-up / travel).'),
        ),
        migrations.AddField(
            model_name='srpprogram',
            name='fleet_op_default_duration_minutes',
            field=models.PositiveIntegerField(default=120, help_text='Assumed op length when an operation has no explicit duration.'),
        ),
        migrations.AddField(
            model_name='srpprogram',
            name='fleet_op_require_attendance',
            field=models.BooleanField(default=False, help_text="Also require the pilot's recorded attendance (PAP) on that op, not just the window."),
        ),
    ]
