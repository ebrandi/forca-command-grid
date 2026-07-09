from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pilots', '0007_pilotpreference_notify_idle_queue'),
    ]

    operations = [
        migrations.AlterField(
            model_name='contributionevent',
            name='kind',
            field=models.CharField(
                choices=[
                    ('build', 'Built'), ('haul', 'Hauled'), ('task', 'Completed task'),
                    ('srp', 'Ship replacement'), ('mining', 'Mined'), ('fleet', 'Flew in fleet'),
                    ('train', 'Trained skill'), ('doctrine', 'Unlocked doctrine'),
                    ('directive', 'Completed directive'),
                ],
                db_index=True, max_length=12,
            ),
        ),
    ]
