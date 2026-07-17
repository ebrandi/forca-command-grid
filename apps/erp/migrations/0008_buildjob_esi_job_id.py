from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("erp", "0007_buildjob_i18n_keys"),
    ]

    operations = [
        migrations.AddField(
            model_name="buildjob",
            name="esi_job_id",
            field=models.BigIntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="buildjob",
            constraint=models.UniqueConstraint(
                condition=models.Q(("esi_job_id__isnull", False)),
                fields=("esi_job_id",),
                name="uniq_buildjob_esi_job_id",
            ),
        ),
    ]
