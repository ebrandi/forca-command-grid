"""Trigram (pg_trgm) GIN indexes on SDE name columns (audit M5).

The member-facing autocompletes (apps/sde/search.py: search_types / search_ships /
search_skills / search_systems / search_stations) all filter with name__icontains
(ILIKE '%q%'), which a plain btree cannot serve — so they sequentially scan the SDE
tables (SdeType ~52k rows, plus systems/stations). A pg_trgm GIN index makes these
index scans.

CREATE EXTENSION pg_trgm is a one-time operation; the app DB role owns its schema in
this deployment (verified: the extension + a gin_trgm_ops index build succeed). These
are STATIC reference tables (rewritten only by load_sde), so GIN write cost is irrelevant.
Fully reversible (drops the indexes; the extension is left installed, which is harmless).
"""
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("sde", "0009_sdedecryptor_sdeblueprintactivitytime_and_more"),
    ]

    operations = [
        TrigramExtension(),
        migrations.AddIndex(
            model_name="sdetype",
            index=GinIndex(
                fields=["name"], name="sde_type_name_trgm", opclasses=["gin_trgm_ops"]
            ),
        ),
        migrations.AddIndex(
            model_name="sdesolarsystem",
            index=GinIndex(
                fields=["name"], name="sde_system_name_trgm", opclasses=["gin_trgm_ops"]
            ),
        ),
        migrations.AddIndex(
            model_name="sdestation",
            index=GinIndex(
                fields=["name"], name="sde_station_name_trgm", opclasses=["gin_trgm_ops"]
            ),
        ),
    ]
