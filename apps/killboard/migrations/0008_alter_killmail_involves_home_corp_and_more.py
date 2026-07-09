"""Drop redundant single-column indexes on the two biggest tables.

Performance audit R1/R2/R3 (handbooks/reference/database.md):
- KillmailParticipant.killmail / KillmailItem.killmail: the auto FK index is redundant
  with the unique_together (killmail_id, role, seq) / (killmail_id, idx) that already
  serves every WHERE killmail_id=? and FK join. Removing it drops one index-maintenance
  write per inserted participant/item on the 5.5M / 3.5M-row append-heavy tables.
- Killmail.involves_home_corp: redundant with the (involves_home_corp, home_corp_role,
  killmail_time DESC) composite whose leading column it is.

Index-only migration: the ORM is unaffected (a dropped redundant index is transparent).
In production the generated DROP INDEX is metadata-only and near-instant; run it as
DROP INDEX CONCURRENTLY in a busy import window if a zero-lock guarantee is wanted.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("killboard", "0007_fitdeviation"),
    ]

    operations = [
        migrations.AlterField(
            model_name="killmail",
            name="involves_home_corp",
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name="killmailitem",
            name="killmail",
            field=models.ForeignKey(
                db_index=False,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="items",
                to="killboard.killmail",
            ),
        ),
        migrations.AlterField(
            model_name="killmailparticipant",
            name="killmail",
            field=models.ForeignKey(
                db_index=False,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="participants",
                to="killboard.killmail",
            ),
        ),
    ]
