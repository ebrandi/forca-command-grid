from django.conf import settings
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("operations", "0005_sovstructure"),
    ]

    operations = [
        # --- New Operation fields ------------------------------------------------
        migrations.AddField(
            model_name="operation",
            name="duration_minutes",
            field=models.PositiveIntegerField(blank=True, help_text="Expected duration in minutes.", null=True),
        ),
        migrations.AddField(
            model_name="operation",
            name="formup",
            field=models.CharField(blank=True, help_text="Form-up / staging location.", max_length=200),
        ),
        migrations.AddField(
            model_name="operation",
            name="destination",
            field=models.CharField(blank=True, help_text="Destination or target area.", max_length=200),
        ),
        migrations.AddField(
            model_name="operation",
            name="comms",
            field=models.CharField(blank=True, help_text="Comms channel / Mumble / Discord voice.", max_length=200),
        ),
        migrations.AddField(
            model_name="operation",
            name="link",
            field=models.CharField(blank=True, help_text="Doctrine, fitting or external link.", max_length=500),
        ),
        migrations.AddField(
            model_name="operation",
            name="min_pilots",
            field=models.PositiveIntegerField(default=0, help_text="Confirmed pilots needed to run."),
        ),
        migrations.AddField(
            model_name="operation",
            name="rsvp_deadline",
            field=models.DateTimeField(
                blank=True, help_text="Sign-up cut-off (EVE/UTC); must be before form-up.", null=True
            ),
        ),
        migrations.AddField(
            model_name="operation",
            name="rsvp_offset_minutes",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="If set, the deadline tracks this many minutes before form-up.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="operation",
            name="srp",
            field=models.CharField(
                blank=True,
                choices=[
                    ("alliance", "Alliance SRP"),
                    ("corp", "Corp SRP"),
                    ("organiser", "Organiser-funded SRP"),
                    ("none", "No SRP coverage"),
                ],
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="operation",
            name="requirements_overridden",
            field=models.BooleanField(
                default=False, help_text="Organiser confirmed the op runs even if the minimum isn't met."
            ),
        ),
        migrations.AddField(
            model_name="operation",
            name="override_note",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="operation",
            name="fc",
            field=models.ForeignKey(
                blank=True,
                help_text="Fleet commander / organiser.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        # --- Choice / width changes on existing fields ---------------------------
        migrations.AlterField(
            model_name="operation",
            name="type",
            field=models.CharField(
                choices=[
                    ("pvp", "PvP fleet"),
                    ("roam", "Roaming gang"),
                    ("gatecamp", "Gate camp"),
                    ("ratting", "Ratting fleet"),
                    ("mining", "Mining operation"),
                    ("logistics", "Transport / logistics"),
                    ("deployment", "Deployment"),
                    ("war_prep", "War preparation"),
                    ("home_defence", "Home defence"),
                    ("structure_timer", "Structure timer"),
                    ("doctrine_rollout", "Doctrine rollout"),
                    ("industrial", "Industrial campaign"),
                ],
                default="pvp",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="operation",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "Draft"),
                    ("planned", "Scheduled"),
                    ("active", "Active"),
                    ("done", "Completed"),
                    ("cancelled", "Cancelled (manual)"),
                    ("cancelled_auto", "Cancelled — too few sign-ups"),
                ],
                db_index=True,
                default="planned",
                max_length=15,
            ),
        ),
        # --- New models ----------------------------------------------------------
        migrations.CreateModel(
            name="OperationShipSlot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ship_name", models.CharField(max_length=200)),
                ("ship_type_id", models.BigIntegerField(blank=True, null=True)),
                (
                    "role",
                    models.CharField(
                        choices=[
                            ("dps", "DPS"),
                            ("logi", "Logistics"),
                            ("tackle", "Tackle"),
                            ("scout", "Scout"),
                            ("booster", "Booster"),
                            ("hauler", "Hauler"),
                            ("miner", "Miner"),
                            ("command", "Command ship"),
                            ("ewar", "EWAR"),
                            ("other", "Other"),
                        ],
                        default="dps",
                        max_length=10,
                    ),
                ),
                ("min_pilots", models.PositiveIntegerField(default=1, help_text="Pilots required on this ship.")),
                (
                    "max_pilots",
                    models.PositiveIntegerField(blank=True, help_text="Optional hard cap (blank = no cap).", null=True),
                ),
                ("priority", models.PositiveIntegerField(default=1, help_text="1 = most needed; shown first.")),
                ("fitting_link", models.CharField(blank=True, max_length=500)),
                ("notes", models.CharField(blank=True, max_length=200)),
                (
                    "operation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ship_slots",
                        to="operations.operation",
                    ),
                ),
            ],
            options={"ordering": ["priority", "id"]},
        ),
        migrations.CreateModel(
            name="OperationCommitment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("character_name", models.CharField(blank=True, max_length=200)),
                (
                    "operation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="commitments",
                        to="operations.operation",
                    ),
                ),
                (
                    "slot",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="commitments",
                        to="operations.operationshipslot",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="op_commitments",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["created_at"], "unique_together": {("operation", "user")}},
        ),
        migrations.CreateModel(
            name="OperationCancellation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("operation_pk", models.IntegerField(db_index=True)),
                ("operation_type", models.CharField(max_length=20)),
                ("organiser_name", models.CharField(blank=True, max_length=200)),
                ("scheduled_start", models.DateTimeField(blank=True, null=True)),
                ("rsvp_deadline", models.DateTimeField(blank=True, null=True)),
                ("min_pilots", models.PositiveIntegerField(default=0)),
                ("confirmed_at_deadline", models.PositiveIntegerField(default=0)),
                ("required_composition", models.JSONField(blank=True, default=dict)),
                ("actual_composition", models.JSONField(blank=True, default=dict)),
                (
                    "reason",
                    models.CharField(
                        choices=[
                            ("insufficient_signups", "Too few sign-ups"),
                            ("composition_unmet", "Doctrine composition not met"),
                            ("manual", "Cancelled by organiser"),
                        ],
                        max_length=24,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "operation",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="cancellations",
                        to="operations.operation",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
