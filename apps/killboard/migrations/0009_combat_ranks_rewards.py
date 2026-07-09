"""Combat rank titles, rewards and the monthly per-pilot ranking aggregate.

Hand-authored (the container does not run makemigrations), additive and
non-destructive: five new tables, no change to any existing column. The default
17-rung rank ladder is seeded create-only, so re-running or an already-populated
ladder is left untouched.
"""
from __future__ import annotations

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


# The seeded default ladder (leaders retune it in the Admin Console). Kept in the
# migration so a fresh install has a working, motivating ladder from day one.
DEFAULT_LADDER = [
    (0, "Dockside Recruit", "text-faint", "Every legend starts here. Undock and make your mark."),
    (1, "First Blood", "text-muted", "Your first confirmed kill — welcome to the fight."),
    (5, "Skirmisher", "text-muted", "Five down. You're finding your range."),
    (10, "Line Pilot", "text-cyan", "A dependable body on the field."),
    (25, "Combat Wingman", "text-cyan", "Reliable in a gang — the corp counts on you."),
    (50, "Proven Combatant", "text-cyan", "Fifty kills of proof you belong on grid."),
    (100, "Battle-Tested Pilot", "text-gold", "A hundred kills. The enemy knows your name."),
    (250, "Fleet Regular", "text-gold", "Always on the fleet, always in the mix."),
    (500, "Veteran Combatant", "text-gold", "Five hundred kills of hard-won experience."),
    (1000, "Ace Pilot", "text-kill", "Four digits. A genuine corp asset."),
    (1500, "Elite Ace", "text-kill", "Among the sharpest blades in the corp."),
    (2500, "Vanguard Hunter", "text-kill", "You lead from the front of every roam."),
    (5000, "War Machine", "text-kill", "Five thousand kills. A one-pilot problem."),
    (7500, "Command Grid Enforcer", "text-kill", "The grid is yours to hold."),
    (10000, "FORCA Warlord", "text-kill", "Ten thousand kills. A pillar of corp history."),
    (15000, "Campaign Legend", "text-kill", "Your name is written across a decade of wars."),
    (25000, "Immortal of FORCA", "text-kill", "The summit. Almost no one will ever stand here."),
]


def seed_ladder(apps, schema_editor):
    CombatRankTitle = apps.get_model("killboard", "CombatRankTitle")
    if CombatRankTitle.objects.exists():
        return  # create-only: never clobber a configured/edited ladder
    CombatRankTitle.objects.bulk_create([
        CombatRankTitle(
            name=name, metric="kills", min_kills=threshold, description=desc,
            color_class=color, sort_order=i, is_active=True, is_visible=True,
            grants_reward=False, reward_type="none", reward_amount=0,
        )
        for i, (threshold, name, color, desc) in enumerate(DEFAULT_LADDER)
    ])


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("killboard", "0008_alter_killmail_involves_home_corp_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="CombatRankTitle",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(help_text="The title a pilot earns, e.g. “Line Pilot”.", max_length=64)),
                ("metric", models.CharField(choices=[("kills", "All-time PvP kills"), ("solo_kills", "Solo kills"), ("final_blows", "Final blows"), ("points", "Points"), ("isk_destroyed", "ISK destroyed"), ("active_days", "Active days")], default="kills", help_text="Which stat the threshold measures. Only “All-time PvP kills” is live today.", max_length=16)),
                ("min_kills", models.PositiveIntegerField(default=0, help_text="Minimum value of the metric to hold this title (0 = the entry-level rank).")),
                ("description", models.CharField(blank=True, max_length=200)),
                ("badge_icon", models.CharField(blank=True, help_text="Optional icon symbol id from the sprite sheet (e.g. “i-trophy”).", max_length=32)),
                ("color_class", models.CharField(default="text-faint", help_text="Tailwind text-colour token for the title (e.g. “text-gold”).", max_length=32)),
                ("sort_order", models.PositiveIntegerField(default=0, help_text="Display order (low → high).")),
                ("is_active", models.BooleanField(default=True, help_text="Inactive ranks are excluded from the live ladder.")),
                ("is_visible", models.BooleanField(default=True, help_text="Whether pilots see this rung on the public ladder.")),
                ("grants_reward", models.BooleanField(default=False, help_text="Whether reaching this rank (after baseline) creates a reward.")),
                ("reward_type", models.CharField(choices=[("none", "No reward"), ("isk", "ISK"), ("plex", "PLEX"), ("item", "Item"), ("manual", "Manual / other")], default="none", max_length=8)),
                ("reward_amount", models.DecimalField(decimal_places=2, default=0, help_text="ISK amount, or PLEX quantity, per pilot reaching this rank.", max_digits=20)),
                ("reward_item_type_id", models.IntegerField(blank=True, help_text="For item rewards: the EVE type id.", null=True)),
                ("reward_notes", models.CharField(blank=True, max_length=200)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["metric", "min_kills"],
            },
        ),
        migrations.AddConstraint(
            model_name="combatranktitle",
            constraint=models.UniqueConstraint(fields=("metric", "min_kills"), name="uniq_rank_metric_threshold"),
        ),
        migrations.CreateModel(
            name="RankRewardSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("rewards_enabled", models.BooleanField(default=False, help_text="Master switch. Off = ranks/titles work but no reward events are ever created.")),
                ("baseline_established_at", models.DateTimeField(blank=True, help_text="When the future-only reward baseline was last (re)snapshotted.", null=True)),
                ("monthly_budget", models.DecimalField(decimal_places=2, default=0, help_text="Fallback monthly ISK incentive budget when income data isn't used.", max_digits=20)),
                ("max_income_pct", models.DecimalField(decimal_places=2, default=0, help_text="Max %% of recent monthly corp income to spend on rank rewards (0 = ignore income).", max_digits=5)),
                ("monthly_cap", models.DecimalField(decimal_places=2, default=0, help_text="Hard ceiling on monthly reward liability (0 = no cap).", max_digits=20)),
                ("payout_currency", models.CharField(choices=[("isk", "ISK"), ("plex", "PLEX"), ("manual", "Manual")], default="isk", max_length=8)),
                ("plex_isk_rate", models.DecimalField(decimal_places=2, default=0, help_text="ISK per PLEX override for liability estimates (0 = use live market price).", max_digits=20)),
                ("default_strategy", models.CharField(choices=[("conservative", "Conservative"), ("standard", "Standard"), ("aggressive", "Aggressive")], default="standard", max_length=16)),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="PilotRankBaseline",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("character_id", models.BigIntegerField(db_index=True, unique=True)),
                ("character_name", models.CharField(blank=True, max_length=128)),
                ("baseline_min_kills", models.PositiveIntegerField(default=0, help_text="Threshold of the pilot's highest rank at baseline time.")),
                ("baseline_kills", models.PositiveIntegerField(default=0)),
                ("established_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("baseline_rank", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="killboard.combatranktitle")),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="RankRewardEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("character_id", models.BigIntegerField(db_index=True)),
                ("character_name", models.CharField(blank=True, max_length=128)),
                ("rank_name", models.CharField(max_length=64)),
                ("rank_min_kills", models.PositiveIntegerField(default=0)),
                ("previous_rank_name", models.CharField(blank=True, max_length=64)),
                ("kills_at_award", models.PositiveIntegerField(default=0)),
                ("achieved_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("reward_type", models.CharField(choices=[("none", "No reward"), ("isk", "ISK"), ("plex", "PLEX"), ("item", "Item"), ("manual", "Manual / other")], default="none", max_length=8)),
                ("reward_amount", models.DecimalField(decimal_places=2, default=0, max_digits=20)),
                ("reward_item_type_id", models.IntegerField(blank=True, null=True)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("approved", "Approved"), ("paid", "Paid"), ("rejected", "Rejected"), ("cancelled", "Cancelled")], db_index=True, default="pending", max_length=12)),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
                ("paid_at", models.DateTimeField(blank=True, null=True)),
                ("payment_reference", models.CharField(blank=True, max_length=200)),
                ("notes", models.TextField(blank=True)),
                ("approved_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("paid_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("rank", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="reward_events", to="killboard.combatranktitle")),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="rank_reward_events", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="rankrewardevent",
            constraint=models.UniqueConstraint(fields=("character_id", "rank_min_kills"), name="uniq_reward_char_rank"),
        ),
        migrations.AddIndex(
            model_name="rankrewardevent",
            index=models.Index(fields=["status", "-created_at"], name="rre_status_created_idx"),
        ),
        migrations.CreateModel(
            name="MonthlyPilotKillStat",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("character_id", models.BigIntegerField(db_index=True)),
                ("year", models.PositiveSmallIntegerField()),
                ("month", models.PositiveSmallIntegerField()),
                ("kills", models.IntegerField(default=0)),
                ("losses", models.IntegerField(default=0)),
                ("solo_kills", models.IntegerField(default=0)),
                ("final_blows", models.IntegerField(default=0)),
                ("isk_destroyed", models.DecimalField(decimal_places=2, default=0, max_digits=24)),
                ("isk_lost", models.DecimalField(decimal_places=2, default=0, max_digits=24)),
                ("points", models.IntegerField(default=0)),
                ("active_days", models.IntegerField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.AddConstraint(
            model_name="monthlypilotkillstat",
            constraint=models.UniqueConstraint(fields=("character_id", "year", "month"), name="uniq_monthly_pilot_period"),
        ),
        migrations.AddIndex(
            model_name="monthlypilotkillstat",
            index=models.Index(fields=["year", "month"], name="mpks_year_month_idx"),
        ),
        migrations.AddIndex(
            model_name="monthlypilotkillstat",
            index=models.Index(fields=["year", "month", "-kills"], name="mpks_ym_kills_idx"),
        ),
        migrations.RunPython(seed_ladder, migrations.RunPython.noop),
    ]
