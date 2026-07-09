from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("navigation", "0002_cynobeacon_ansiblexbridge_source_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="JumpPlannerConfig",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("is_active", models.BooleanField(default=True)),
                ("enabled", models.BooleanField(default=True)),
                ("default_jdc", models.PositiveSmallIntegerField(default=5)),
                ("default_jfc", models.PositiveSmallIntegerField(default=5)),
                ("default_jf_skill", models.PositiveSmallIntegerField(default=5)),
                ("prefer_stations", models.BooleanField(default=True)),
                ("default_preference", models.CharField(
                    choices=[("safer", "Safer (prefer high-sec)"), ("shortest", "Shortest"),
                             ("insecure", "Less secure (prefer low/null)")],
                    default="safer", max_length=12)),
                ("fuel_safety_margin_pct", models.FloatField(default=0.0)),
                ("avoid_systems", models.TextField(blank=True)),
                ("avoid_regions", models.TextField(blank=True)),
                ("allow_pilot_exit_override", models.BooleanField(default=True)),
                ("allow_saved_routes", models.BooleanField(default=True)),
                ("highsec_exit_warning", models.TextField(
                    blank=True,
                    default="Low-sec exit systems carry real risk — scout the exit and the gate route, "
                            "and don't autopilot a loaded jump freighter through low-sec.")),
            ],
            options={"verbose_name": "Jump planner config"},
        ),
        migrations.CreateModel(
            name="SavedJumpRoute",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=120)),
                ("origin_system_id", models.IntegerField()),
                ("origin_name", models.CharField(max_length=100)),
                ("dest_system_id", models.IntegerField()),
                ("dest_name", models.CharField(max_length=100)),
                ("ship_key", models.CharField(max_length=24)),
                ("jdc", models.PositiveSmallIntegerField(default=5)),
                ("jfc", models.PositiveSmallIntegerField(default=5)),
                ("jf_skill", models.PositiveSmallIntegerField(default=5)),
                ("jde_rigs", models.PositiveSmallIntegerField(default=0)),
                ("preference", models.CharField(default="safer", max_length=12)),
                ("custom_range", models.FloatField(blank=True, null=True)),
                ("waypoints", models.CharField(blank=True, max_length=300)),
                ("avoid_systems", models.CharField(blank=True, max_length=300)),
                ("avoid_regions", models.CharField(blank=True, max_length=300)),
                ("require_stations", models.BooleanField(default=False)),
                ("exit_system_id", models.IntegerField(blank=True, null=True)),
                ("visibility", models.CharField(
                    choices=[("private", "Private (only me)"), ("leadership", "Shared with leadership")],
                    default="private", max_length=12)),
                ("note", models.CharField(blank=True, max_length=200)),
                ("owner", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="saved_jump_routes", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-updated_at"],
            },
        ),
        migrations.AddIndex(
            model_name="savedjumproute",
            index=models.Index(fields=["owner", "-updated_at"], name="navigation__owner_i_32c053_idx"),
        ),
    ]
