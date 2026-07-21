from __future__ import annotations

import secrets

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def _backfill_slugs(apps, schema_editor):
    """Give every pre-existing BattleReport a unique permalink slug (KB-31)."""
    BattleReport = apps.get_model("killboard", "BattleReport")
    used: set[str] = set()
    for report in BattleReport.objects.filter(slug="").only("id"):
        slug = secrets.token_urlsafe(9)
        while slug in used:
            slug = secrets.token_urlsafe(9)
        used.add(slug)
        report.slug = slug
        report.save(update_fields=["slug"])


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("killboard", "0019_killboardsubscription_killboardsubscriptionevent"),
    ]

    operations = [
        # Slug added non-unique first so existing rows can be backfilled with distinct
        # values, then promoted to unique.
        migrations.AddField(
            model_name="battlereport",
            name="slug",
            field=models.CharField(blank=True, default="", max_length=22),
        ),
        migrations.RunPython(_backfill_slugs, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="battlereport",
            name="slug",
            field=models.CharField(blank=True, max_length=22, unique=True),
        ),
        migrations.CreateModel(
            name="BattleReportSide",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("index", models.IntegerField()),
                ("label", models.CharField(blank=True, max_length=40)),
                ("is_home_side", models.BooleanField(default=False)),
                ("kills", models.IntegerField(default=0)),
                ("losses", models.IntegerField(default=0)),
                ("isk_destroyed", models.DecimalField(decimal_places=2, default=0, max_digits=24)),
                ("isk_lost", models.DecimalField(decimal_places=2, default=0, max_digits=24)),
                ("pilot_count", models.IntegerField(default=0)),
                ("report", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="detected_sides",
                    to="killboard.battlereport",
                )),
            ],
            options={
                "ordering": ["report", "index"],
                "unique_together": {("report", "index")},
            },
        ),
        migrations.CreateModel(
            name="BattleReportSideMember",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("entity_type", models.CharField(choices=[
                    ("corporation", "Corporation"),
                    ("alliance", "Alliance"),
                    ("character", "Character"),
                ], max_length=12)),
                ("entity_id", models.BigIntegerField()),
                ("is_manual", models.BooleanField(default=False)),
                ("kills", models.IntegerField(default=0)),
                ("losses", models.IntegerField(default=0)),
                ("isk_lost", models.DecimalField(decimal_places=2, default=0, max_digits=24)),
                ("pilot_count", models.IntegerField(default=0)),
                ("side", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="members",
                    to="killboard.battlereportside",
                )),
            ],
            options={
                "ordering": ["side", "-isk_lost", "entity_id"],
            },
        ),
        migrations.CreateModel(
            name="BattleReportSideOverride",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("entity_type", models.CharField(choices=[
                    ("corporation", "Corporation"),
                    ("alliance", "Alliance"),
                    ("character", "Character"),
                ], max_length=12)),
                ("entity_id", models.BigIntegerField()),
                ("side_index", models.IntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="+",
                    to=settings.AUTH_USER_MODEL,
                )),
                ("report", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="side_overrides",
                    to="killboard.battlereport",
                )),
            ],
            options={
                "unique_together": {("report", "entity_type", "entity_id")},
            },
        ),
    ]
