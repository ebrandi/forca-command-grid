from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("killboard", "0018_killboardstreamevent"),
    ]

    operations = [
        migrations.CreateModel(
            name="KillboardSubscription",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("event_type", models.CharField(choices=[
                    ("my_kill", "One of my pilots got on a kill"),
                    ("my_loss", "One of my pilots died"),
                    ("my_loss_srp_pending", "My loss is eligible for SRP"),
                    ("watchlist_hit", "A watched entity appeared on a killmail"),
                    ("rank_up", "I reached a new combat rank"),
                    ("filter_match", "A kill matched my saved filter"),
                ], max_length=24)),
                ("channel", models.CharField(choices=[
                    ("notify", "In-app + linked chat DMs"),
                    ("email", "Email"),
                    ("webhook", "Webhook (HTTPS POST)"),
                    ("rss", "RSS / Atom feed"),
                ], max_length=12)),
                ("params", models.JSONField(blank=True, default=dict)),
                ("enabled", models.BooleanField(default=True)),
                ("webhook_url", models.URLField(blank=True, max_length=500)),
                ("rss_token", models.CharField(blank=True, max_length=64, null=True, unique=True)),
                ("last_seq", models.BigIntegerField(default=0)),
                ("last_fired", models.DateTimeField(blank=True, null=True)),
                ("consecutive_failures", models.IntegerField(default=0)),
                ("disabled_reason", models.CharField(blank=True, max_length=200)),
                ("user", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="killboard_subscriptions",
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="KillboardSubscriptionEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_type", models.CharField(choices=[
                    ("my_kill", "One of my pilots got on a kill"),
                    ("my_loss", "One of my pilots died"),
                    ("my_loss_srp_pending", "My loss is eligible for SRP"),
                    ("watchlist_hit", "A watched entity appeared on a killmail"),
                    ("rank_up", "I reached a new combat rank"),
                    ("filter_match", "A kill matched my saved filter"),
                ], max_length=24)),
                ("killmail_id", models.BigIntegerField(blank=True, null=True)),
                ("seq", models.BigIntegerField(blank=True, null=True)),
                ("title", models.CharField(max_length=200)),
                ("summary", models.CharField(blank=True, max_length=500)),
                ("link", models.CharField(blank=True, max_length=300)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("subscription", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="feed_events",
                    to="killboard.killboardsubscription",
                )),
            ],
            options={
                "ordering": ["-created"],
            },
        ),
        migrations.AddIndex(
            model_name="killboardsubscription",
            index=models.Index(fields=["event_type", "enabled"], name="kbsub_type_enabled_idx"),
        ),
        migrations.AddIndex(
            model_name="killboardsubscription",
            index=models.Index(fields=["user", "-created_at"], name="kbsub_user_created_idx"),
        ),
        migrations.AddIndex(
            model_name="killboardsubscriptionevent",
            index=models.Index(fields=["subscription", "-created"], name="kbsubev_sub_created_idx"),
        ),
    ]
