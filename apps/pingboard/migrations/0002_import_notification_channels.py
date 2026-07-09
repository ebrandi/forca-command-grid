"""Copy the legacy Discord webhook registry into Pingboard's provider table.

Phase 0 is additive: this seeds ``ChannelProvider`` from
``recommendations.NotificationChannel`` so Pingboard can send Discord independently,
while the legacy ``broadcast_discord`` path keeps reading ``NotificationChannel``
unchanged. Idempotent (dedup on the imported label) and reversible (drops only the
rows it created).
"""
from __future__ import annotations

from django.db import migrations

_IMPORT_PREFIX = "Imported Discord #"


def copy_channels(apps, schema_editor):
    NotificationChannel = apps.get_model("recommendations", "NotificationChannel")
    ChannelProvider = apps.get_model("pingboard", "ChannelProvider")
    from core.esi.tokens import encrypt

    for ch in NotificationChannel.objects.filter(kind="discord_webhook"):
        url = (ch.config or {}).get("url", "")
        if not url:
            continue
        label = f"{_IMPORT_PREFIX}{ch.pk}"
        if ChannelProvider.objects.filter(kind="discord", label=label).exists():
            continue  # idempotent
        try:
            secret = encrypt(url)
        except Exception:  # noqa: BLE001 - never let key config abort the migration
            continue
        ChannelProvider.objects.create(
            kind="discord",
            label=label,
            enabled=bool(ch.active),
            is_default=False,
            supports_channel=True,
            _secret=secret,
        )


def drop_imported(apps, schema_editor):
    ChannelProvider = apps.get_model("pingboard", "ChannelProvider")
    ChannelProvider.objects.filter(kind="discord", label__startswith=_IMPORT_PREFIX).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("pingboard", "0001_initial"),
        ("recommendations", "0001_initial"),
    ]

    operations = [migrations.RunPython(copy_channels, drop_imported)]
