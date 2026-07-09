from django.db import migrations, models


def _freeze_legacy_done(apps, schema_editor):
    """Future-only: directives already DONE before 3.6 must never earn retroactive credit or
    raffle tickets. Stamp their completed_at to updated_at (a past time) so the credit guard
    (completed_at is None) blocks them and they fall outside any future raffle window."""
    M = apps.get_model("command_intel", "PilotDirective")
    M.objects.filter(state="done", completed_at__isnull=True).update(
        completed_at=models.F("updated_at")
    )


class Migration(migrations.Migration):

    dependencies = [
        ('command_intel', '0005_battle_analysis'),
    ]

    operations = [
        migrations.AddField(
            model_name='pilotdirective',
            name='completed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(_freeze_legacy_done, migrations.RunPython.noop),
    ]
