"""Close out member-permission recommendations left open by the retired evaluators.

The Daily Briefing merge removed the member surface for Recommendation rows
(newbro_next_step / skill_training duplicated the quest log and Getting-started
section), and their evaluators no longer run — without this, the last open row
per (type, subject) would sit NEW forever with no way to close it.
"""
from django.db import migrations


def close_open_member_recs(apps, schema_editor):
    Recommendation = apps.get_model("recommendations", "Recommendation")
    Recommendation.objects.filter(
        required_permission="member", state__in=["new", "acknowledged"]
    ).update(state="superseded")


class Migration(migrations.Migration):
    dependencies = [
        ("recommendations", "0004_recommendation_isk_impact"),
    ]

    operations = [
        migrations.RunPython(close_open_member_recs, migrations.RunPython.noop),
    ]
