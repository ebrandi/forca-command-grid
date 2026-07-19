"""Widen FitRevision.data_version 32 -> 128.

The composite eval data version grew new tokens ("+dg"/"+gs1" for the dogma graph + graph-skills
flag) and now exceeds 32 chars, so stamping it on a new revision (create/import a fit) raised
`DataError: value too long for type character varying(32)`. Hand-written (the container cannot
makemigrations against the mounted host dir).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fitting", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="fitrevision",
            name="data_version",
            field=models.CharField(blank=True, max_length=128),
        ),
    ]
