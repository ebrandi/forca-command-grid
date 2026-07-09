# Generated for the configurable kill-feed pings (leadership gap #6).

from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("killboard", "0004_killmail_perf_indexes"),
    ]

    operations = [
        migrations.CreateModel(
            name="KillFeedConfig",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("enabled", models.BooleanField(default=False)),
                ("min_loss_value", models.DecimalField(decimal_places=2, default=Decimal("100000000"), help_text="Post a corp loss when its value is at least this (0 = off).", max_digits=20)),
                ("min_kill_value", models.DecimalField(decimal_places=2, default=Decimal("500000000"), help_text="Post a corp kill when its value is at least this (0 = off).", max_digits=20)),
            ],
            options={"abstract": False},
        ),
        migrations.CreateModel(
            name="KillFeedPing",
            fields=[
                ("killmail_id", models.BigIntegerField(primary_key=True, serialize=False)),
                ("posted_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
    ]
