from decimal import Decimal

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("buyback", "0002_ore_mode"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="GuaranteedBuybackConfig",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("enabled", models.BooleanField(default=False)),
                ("audience", models.CharField(choices=[("public", "Public — anyone can use it"), ("alliance", "Corp & alliance members only"), ("corp", "Corp members only"), ("disabled", "Disabled")], default="disabled", max_length=10)),
                ("per_lot_cap", models.DecimalField(decimal_places=2, default=Decimal("100000000"), max_digits=20)),
                ("daily_budget", models.DecimalField(decimal_places=2, default=Decimal("1000000000"), max_digits=20)),
                ("require_esi_reconcile", models.BooleanField(default=True)),
                ("intro_text", models.TextField(blank=True, default="The corp guarantees to buy your lot at the quoted price. Submit it, an officer approves, then the corp pays you in-game. No ISK moves through this app.")),
            ],
            options={"abstract": False},
        ),
        migrations.CreateModel(
            name="GuaranteedBuyout",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("seller_character_id", models.BigIntegerField(blank=True, null=True)),
                ("items", models.JSONField(blank=True, default=list)),
                ("item_count", models.IntegerField(default=0)),
                ("volume_m3", models.FloatField(default=0.0)),
                ("jita_value", models.DecimalField(decimal_places=2, default=0, max_digits=20)),
                ("quoted_value", models.DecimalField(decimal_places=2, default=0, max_digits=20)),
                ("location_name", models.CharField(blank=True, max_length=200)),
                ("notes", models.CharField(blank=True, max_length=300)),
                ("status", models.CharField(choices=[("requested", "Requested"), ("approved", "Approved — awaiting corp payment"), ("settled", "Settled"), ("rejected", "Rejected"), ("cancelled", "Cancelled")], db_index=True, default="requested", max_length=10)),
                ("decided_at", models.DateTimeField(blank=True, null=True)),
                ("decision_reason", models.CharField(blank=True, max_length=200)),
                ("settled_at", models.DateTimeField(blank=True, null=True)),
                ("settlement_kind", models.CharField(blank=True, choices=[("esi", "ESI wallet match"), ("manual", "Officer-confirmed")], max_length=6)),
                ("settlement_ref", models.CharField(blank=True, max_length=64)),
                ("decided_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="guaranteed_buyout_decisions", to=settings.AUTH_USER_MODEL)),
                ("seller", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="guaranteed_buyouts", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="guaranteedbuyout",
            index=models.Index(fields=["status", "-created_at"], name="buyback_gua_status_idx"),
        ),
    ]
