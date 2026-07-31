"""Partial freshness index for the member-skills health probe.

``apps/admin_audit/health.feed_health`` reports member-skill freshness as the newest
``as_of`` among current snapshots, and counts them. The existing (character, is_latest)
index serves neither — ``is_latest`` is its trailing column. Restricting the new index
to ``is_latest = TRUE`` lets the freshness probe read a single row already in order and
turns the count into an index-only scan over the current snapshots rather than the whole
retained snapshot history.

Deliberately a plain (transactional, reversible) ``AddIndex`` rather than a concurrent
build: this table holds roster-sized data — hundreds of pilots' snapshots, not the
hundreds of thousands the killmail and journal tables carry — so the build is
sub-second and the brief ACCESS EXCLUSIVE lock is cheaper than giving up the migration's
atomicity. Revisit if snapshot retention is ever extended.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("characters", "0004_alter_characterattributes_source_and_more"),
        ("sso", "0007_alter_evecharacter_source"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="characterskillsnapshot",
            index=models.Index(
                condition=models.Q(("is_latest", True)),
                fields=["-as_of"],
                name="skillsnap_latest_as_of_idx",
            ),
        ),
    ]
