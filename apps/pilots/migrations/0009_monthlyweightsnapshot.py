from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pilots", "0008_contribution_directive_kind"),
    ]

    operations = [
        migrations.CreateModel(
            name="MonthlyWeightSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("year", models.PositiveSmallIntegerField()),
                ("month", models.PositiveSmallIntegerField()),
                ("weights", models.JSONField(default=dict)),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.AddConstraint(
            model_name="monthlyweightsnapshot",
            constraint=models.UniqueConstraint(fields=["year", "month"], name="uniq_hof_weight_month"),
        ),
    ]
