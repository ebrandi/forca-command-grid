"""KB-37 (WS-D3) — gamification: trophies, kill-of-the-week, seasonal snapshots.

Hand-authored (the container does not run makemigrations), additive and non-destructive: six new
tables plus a minimal, justified extension of ``RankRewardEvent`` so a trophy reward flows through
the EXISTING reward governance flow rather than a parallel table. The reward table gains
``source`` / ``source_key`` / ``trophy``; existing rank rows are backfilled and the idempotency
unique constraint is migrated from ``(character_id, rank_min_kills)`` to ``(character_id,
source_key)`` so a rank rung and a trophy never collide.
"""
from __future__ import annotations

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


def backfill_source_key(apps, schema_editor):
    """Stamp every existing rank reward event with source=rank + source_key=rank:<min_kills>."""
    RankRewardEvent = apps.get_model("killboard", "RankRewardEvent")
    for ev in RankRewardEvent.objects.all().iterator():
        ev.source = "rank"
        ev.source_key = f"rank:{ev.rank_min_kills}"
        ev.save(update_fields=["source", "source_key"])


REWARD_TYPE_CHOICES = [
    ("none", "No reward"), ("isk", "ISK"), ("plex", "PLEX"),
    ("item", "Item"), ("manual", "Manual / other"),
]
TROPHY_CATEGORY_CHOICES = [
    ("kills", "Kills"), ("solo", "Solo"), ("value", "ISK value"), ("ship_class", "Ship class"),
    ("sec_band", "Security band"), ("role", "Battle role"), ("special", "Special"),
]
TROPHY_TIER_CHOICES = [("bronze", "Bronze"), ("silver", "Silver"), ("gold", "Gold")]
REWARD_SOURCE_CHOICES = [("rank", "Combat rank"), ("trophy", "Trophy")]
# The subscription event-type choices gain trophy_awarded (a pushed, non-killmail event).
SUBSCRIPTION_EVENT_CHOICES = [
    ("my_kill", "One of my pilots got on a kill"),
    ("my_loss", "One of my pilots died"),
    ("my_loss_srp_pending", "My loss is eligible for SRP"),
    ("watchlist_hit", "A watched entity appeared on a killmail"),
    ("rank_up", "I reached a new combat rank"),
    ("trophy_awarded", "I earned a trophy"),
    ("filter_match", "A kill matched my saved filter"),
]


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("killboard", "0022_killmail_value_at_kill"),
    ]

    operations = [
        migrations.CreateModel(
            name="TrophyDefinition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("slug", models.SlugField(max_length=64, unique=True)),
                ("name", models.CharField(help_text="The trophy title a pilot earns.", max_length=96)),
                ("description", models.CharField(blank=True, max_length=240)),
                ("category", models.CharField(choices=TROPHY_CATEGORY_CHOICES, default="special", max_length=16)),
                ("tier", models.CharField(choices=TROPHY_TIER_CHOICES, default="bronze", max_length=8)),
                ("criteria", models.JSONField(default=dict)),
                ("badge_icon", models.CharField(blank=True, max_length=32)),
                ("color_class", models.CharField(default="text-gold", max_length=32)),
                ("sort_order", models.PositiveIntegerField(default=0)),
                ("enabled", models.BooleanField(default=True, help_text="Disabled trophies are never evaluated or awarded.")),
                ("grants_reward", models.BooleanField(default=False)),
                ("reward_type", models.CharField(choices=REWARD_TYPE_CHOICES, default="none", max_length=8)),
                ("reward_amount", models.DecimalField(decimal_places=2, default=0, max_digits=20)),
                ("reward_item_type_id", models.IntegerField(blank=True, null=True)),
                ("reward_notes", models.CharField(blank=True, max_length=200)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["sort_order", "category", "slug"]},
        ),
        migrations.AddIndex(
            model_name="trophydefinition",
            index=models.Index(fields=["enabled", "category"], name="trophy_enabled_cat_idx"),
        ),
        migrations.CreateModel(
            name="PilotTrophy",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("character_id", models.BigIntegerField(db_index=True)),
                ("character_name", models.CharField(blank=True, max_length=128)),
                ("awarded_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("notified", models.BooleanField(default=True)),
                ("killmail_id", models.BigIntegerField(blank=True, null=True)),
                ("progress", models.JSONField(blank=True, default=dict)),
                ("definition", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="awards", to="killboard.trophydefinition")),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-awarded_at"]},
        ),
        migrations.AddConstraint(
            model_name="pilottrophy",
            constraint=models.UniqueConstraint(fields=("character_id", "definition"), name="uniq_pilot_trophy"),
        ),
        migrations.CreateModel(
            name="PilotTrophyBaseline",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("character_id", models.BigIntegerField(unique=True)),
            ],
            options={"abstract": False},
        ),
        migrations.CreateModel(
            name="TrophyScanState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("last_seq", models.BigIntegerField(default=0)),
            ],
            options={"abstract": False},
        ),
        migrations.CreateModel(
            name="KillOfTheWeek",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("iso_year", models.PositiveSmallIntegerField()),
                ("iso_week", models.PositiveSmallIntegerField()),
                ("value", models.DecimalField(decimal_places=2, default=0, max_digits=20)),
                ("points", models.IntegerField(default=0)),
                ("character_id", models.BigIntegerField(blank=True, db_index=True, null=True)),
                ("is_override", models.BooleanField(default=False)),
                ("overridden_at", models.DateTimeField(blank=True, null=True)),
                ("notified_at", models.DateTimeField(blank=True, null=True)),
                ("killmail", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="killboard.killmail")),
                ("overridden_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-iso_year", "-iso_week"]},
        ),
        migrations.AddConstraint(
            model_name="killoftheweek",
            constraint=models.UniqueConstraint(fields=("iso_year", "iso_week"), name="uniq_kotw_week"),
        ),
        migrations.CreateModel(
            name="SeasonSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("year", models.PositiveSmallIntegerField()),
                ("quarter", models.PositiveSmallIntegerField()),
                ("boards", models.JSONField(default=dict)),
                ("pilot_count", models.IntegerField(default=0)),
            ],
            options={"ordering": ["-year", "-quarter"]},
        ),
        migrations.AddConstraint(
            model_name="seasonsnapshot",
            constraint=models.UniqueConstraint(fields=("year", "quarter"), name="uniq_season_snapshot"),
        ),
        # --- RankRewardEvent: minimal extension for trophy-sourced rewards ---
        migrations.AddField(
            model_name="rankrewardevent",
            name="source",
            field=models.CharField(choices=REWARD_SOURCE_CHOICES, db_index=True, default="rank", max_length=8),
        ),
        migrations.AddField(
            model_name="rankrewardevent",
            name="source_key",
            field=models.CharField(blank=True, default="", max_length=48),
        ),
        migrations.AddField(
            model_name="rankrewardevent",
            name="trophy",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="reward_events", to="killboard.trophydefinition"),
        ),
        migrations.RunPython(backfill_source_key, migrations.RunPython.noop),
        migrations.RemoveConstraint(model_name="rankrewardevent", name="uniq_reward_char_rank"),
        migrations.AddConstraint(
            model_name="rankrewardevent",
            constraint=models.UniqueConstraint(fields=("character_id", "source_key"), name="uniq_reward_char_source"),
        ),
        # The new trophy_awarded subscription event type widens two choices lists (no DB change).
        migrations.AlterField(
            model_name="killboardsubscription",
            name="event_type",
            field=models.CharField(choices=SUBSCRIPTION_EVENT_CHOICES, max_length=24),
        ),
        migrations.AlterField(
            model_name="killboardsubscriptionevent",
            name="event_type",
            field=models.CharField(choices=SUBSCRIPTION_EVENT_CHOICES, max_length=24),
        ),
    ]
